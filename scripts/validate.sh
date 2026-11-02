#!/usr/bin/env bash
# End-to-end check of the multi-device monitoring demo.
#
#   scripts/validate.sh [--down] [--help]
#
# Starts the stack (docker compose up -d), runs two device agents, and proves:
#   - both devices show fresh telemetry in VictoriaMetrics (and through Grafana)
#   - each has its own identity (device_id / instance labels)
#   - stopping one agent makes it OFFLINE (age > 30 s) while the other stays online
#   - the provisioned Grafana alert DeviceOffline fires for the stopped device only
#
# Exits 0 only if every step passes. Never runs `docker compose down -v`.
# The stack is left running unless --down is given (then plain `down`, data kept).
#
# Environment overrides (defaults in brackets):
#   VM_URL [http://localhost:8428]   GRAFANA_URL [http://localhost:3000]
#   GRAFANA_AUTH [admin:admin]       OTLP_ENDPOINT [localhost:4317]
#   PYTHON [.venv/bin/python if present, else python3]
#   DEVICE_A [demo-pi-1]  DEVICE_B [demo-pi-2]  AGENT_SCRIPT [agent/device_agent.py]
#   STACK_TIMEOUT [180] ONLINE_TIMEOUT [90] OFFLINE_TIMEOUT [90] ALERT_TIMEOUT [180]  (seconds)
#   SKIP_GRAFANA=1   skip Grafana provisioning and alert checks (data path only)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

VM_URL="${VM_URL:-http://localhost:8428}"
GRAFANA_URL="${GRAFANA_URL:-http://localhost:3000}"
GRAFANA_AUTH="${GRAFANA_AUTH:-admin:admin}"
OTLP_ENDPOINT="${OTLP_ENDPOINT:-localhost:4317}"
DEVICE_A="${DEVICE_A:-demo-pi-1}"
DEVICE_B="${DEVICE_B:-demo-pi-2}"
AGENT_SCRIPT="${AGENT_SCRIPT:-agent/device_agent.py}"
STACK_TIMEOUT="${STACK_TIMEOUT:-180}"
ONLINE_TIMEOUT="${ONLINE_TIMEOUT:-90}"
OFFLINE_TIMEOUT="${OFFLINE_TIMEOUT:-90}"
ALERT_TIMEOUT="${ALERT_TIMEOUT:-180}"
SKIP_GRAFANA="${SKIP_GRAFANA:-0}"

FRESH_SECONDS=15     # "fresh": age below this
OFFLINE_SECONDS=30   # contract: OFFLINE when age is above this
ALERT_NAME="DeviceOffline"
DS_UID="victoriametrics"
DASHBOARD_UID="device-fleet"
ALWAYS_PRESENT_METRICS=(
  device_cpu_utilization_percent
  device_memory_utilization_percent
  device_disk_utilization_percent
  device_uptime_seconds
  device_report_interval_seconds   # the agent declares its own interval (docs/CONTRACT.md)
)

DO_DOWN=0
LOG_DIR=""
PIDS=()
PID_A=""
PID_B=""
STARTED_PID=""
STEPS_RUN=0
STEPS_PASSED=0
FAILED_STEP=""
FAILED_REASON=""
DETAIL=""
PY=""

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
  C_PASS=$'\033[32m'; C_FAIL=$'\033[31m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
  C_PASS=""; C_FAIL=""; C_DIM=""; C_OFF=""
fi

note() { printf '%s%s%s\n' "$C_DIM" "$*" "$C_OFF"; }
pass() {
  STEPS_RUN=$((STEPS_RUN + 1)); STEPS_PASSED=$((STEPS_PASSED + 1))
  printf '%sPASS%s  %s\n' "$C_PASS" "$C_OFF" "$1"
}
# fail STEP_NAME REASON: records the failure and exits (the EXIT trap prints the summary).
fail() {
  STEPS_RUN=$((STEPS_RUN + 1))
  FAILED_STEP="$1"; FAILED_REASON="${2:-}"
  printf '%sFAIL%s  %s\n' "$C_FAIL" "$C_OFF" "$1"
  [ -z "${2:-}" ] || printf '      %s\n' "$2"
  exit 1
}

usage() { sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------
# lt A B: true if A < B (floating point). Empty A is treated as "unknown" = false.
lt() { [ -n "${1:-}" ] && awk -v a="$1" -v b="$2" 'BEGIN { exit !(a + 0 < b + 0) }'; }
gt() { [ -n "${1:-}" ] && awk -v a="$1" -v b="$2" 'BEGIN { exit !(a + 0 > b + 0) }'; }

# wait_for TIMEOUT INTERVAL CHECK...  runs CHECK until it returns 0.
#   check rc 0 = success, 1 = not yet, 2 = abort now (reason in $DETAIL)
#   wait_for returns 0 = success, 1 = timeout, 2 = aborted by the check
wait_for() {
  local timeout=$1 interval=$2; shift 2
  local start=$SECONDS next_note=15 rc elapsed
  while :; do
    rc=0
    "$@" || rc=$?
    case $rc in
      0) return 0 ;;
      2) return 2 ;;
    esac
    elapsed=$((SECONDS - start))
    if [ "$elapsed" -ge "$timeout" ]; then return 1; fi
    if [ "$elapsed" -ge "$next_note" ]; then
      note "      ... still waiting (${elapsed}s of ${timeout}s): ${DETAIL}"
      next_note=$((next_note + 15))
    fi
    sleep "$interval"
  done
}

# wait_result STEP_NAME RC TIMEOUT_MESSAGE: turn a wait_for result into pass/fail.
wait_result() {
  case $2 in
    0) pass "$1" ;;
    2) fail "$1" "aborted: ${DETAIL}" ;;
    *) fail "$1" "timed out: ${3}. Last state: ${DETAIL}" ;;
  esac
}

agent_log() { printf '%s/%s.log' "$LOG_DIR" "$1"; }

# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
# Instant query against any Prometheus-compatible endpoint. Args: URL QUERY [curl auth args...]
prom_query() {
  local url=$1 query=$2; shift 2
  curl -fsS --max-time 10 -G "$@" --data-urlencode "query=${query}" "$url"
}

vm_query() { prom_query "${VM_URL}/api/v1/query" "$1"; }
grafana_ds_query() {
  prom_query "${GRAFANA_URL}/api/datasources/proxy/uid/${DS_UID}/api/v1/query" "$1" -u "$GRAFANA_AUTH"
}

# Print "device_id value" lines for an instant-vector result on stdin.
PARSE_VECTOR='
import json, sys
d = json.load(sys.stdin)
if d.get("status") != "success":
    sys.exit("query status: %s" % d.get("status"))
for r in d["data"]["result"]:
    print(r["metric"].get("device_id", "?"), r["value"][1])
'

# Age in seconds of the newest sample of METRIC per device, exactly as in docs/CONTRACT.md.
# $1 = metric name, $2 = query function (vm_query or grafana_ds_query)
ages_of() {
  "$2" "time() - tlast_over_time(${1}[1d])" | "$PY" -c "$PARSE_VECTOR"
}

# age_for DEVICE: read an "ages" listing (device_id age) from $AGES.
age_for() { printf '%s\n' "$AGES" | awk -v d="$1" '$1 == d { print $2; exit }'; }

# Identity check: one series per device, instance == device_id, job == device-agent.
PARSE_IDENTITY='
import json, sys
want = sys.argv[1:]
d = json.load(sys.stdin)
if d.get("status") != "success":
    sys.exit("query status: %s" % d.get("status"))
by_dev = {}
for r in d["data"]["result"]:
    m = r["metric"]
    by_dev.setdefault(m.get("device_id", "?"), []).append(m)
problems = []
for dev in want:
    series = by_dev.get(dev, [])
    if len(series) != 1:
        problems.append("%s: expected 1 uptime series, found %d" % (dev, len(series)))
        continue
    m = series[0]
    if m.get("instance") != dev:
        problems.append("%s: instance label is %r, expected %r" % (dev, m.get("instance"), dev))
    if m.get("job") != "device-agent":
        problems.append("%s: job label is %r, expected device-agent" % (dev, m.get("job")))
    if not m.get("hostname"):
        problems.append("%s: hostname label missing" % dev)
if len(set(want)) != len(want):
    problems.append("device ids are not distinct: %s" % want)
if problems:
    print("; ".join(problems))
    sys.exit(1)
print("ok")
'

# Alert states from Grafana's Prometheus-compatible rules API: "device_id state" lines.
# Exit 3 if the rule itself does not exist.
PARSE_RULE_STATES='
import json, sys
d = json.load(sys.stdin)
name = sys.argv[1]
found = False
for g in d.get("data", {}).get("groups", []):
    for r in g.get("rules", []):
        if r.get("name") == name:
            found = True
            for a in r.get("alerts") or []:
                state = str(a.get("state", "?")).lower().replace(" ", "_")
                print(a.get("labels", {}).get("device_id", "?"), state)
sys.exit(0 if found else 3)
'

# device_ids with an active alert of this name in the Grafana Alertmanager.
PARSE_AM_ALERTS='
import json, sys
name = sys.argv[1]
for a in json.load(sys.stdin):
    if a.get("labels", {}).get("alertname") == name and a.get("status", {}).get("state") == "active":
        print(a["labels"].get("device_id", "?"))
'

grafana_get() { curl -fsS --max-time 10 -u "$GRAFANA_AUTH" "${GRAFANA_URL}$1"; }

# ---------------------------------------------------------------------------
# Checks used with wait_for
# ---------------------------------------------------------------------------
check_vm_health() {
  local out
  DETAIL="GET ${VM_URL}/health"
  out="$(curl -fsS --max-time 5 "${VM_URL}/health" 2>/dev/null)" || return 1
  [ "$out" = OK ]
}

check_grafana_health() {
  DETAIL="GET ${GRAFANA_URL}/api/health"
  curl -fsS --max-time 5 "${GRAFANA_URL}/api/health" 2>/dev/null |
    "$PY" -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("database") == "ok" else 1)' 2>/dev/null
}

check_otlp_port() {
  DETAIL="TCP connect to ${OTLP_ENDPOINT}"
  "$PY" -c '
import socket, sys
host, _, port = sys.argv[1].rpartition(":")
socket.create_connection((host or "localhost", int(port)), 2).close()
' "${OTLP_ENDPOINT#http://}" 2>/dev/null
}

agents_alive() {
  local pid
  for pid in "${PIDS[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      DETAIL="an agent process (pid ${pid}) exited early; see ${LOG_DIR}"
      return 1
    fi
  done
}

# Both devices fresh (< FRESH_SECONDS) for every always-present metric.
check_both_fresh() {
  local m dev age missing="" report=""
  agents_alive || return 2
  for m in "${ALWAYS_PRESENT_METRICS[@]}"; do
    if ! AGES="$(ages_of "$m" vm_query 2>&1)"; then
      DETAIL="VictoriaMetrics query failed: ${AGES}"; return 1
    fi
    for dev in "$DEVICE_A" "$DEVICE_B"; do
      age="$(age_for "$dev")"
      report+="${dev}/${m#device_}=${age:-none} "
      if ! lt "$age" "$FRESH_SECONDS"; then missing+="${dev}/${m} "; fi
    done
  done
  DETAIL="not fresh yet: ${missing:-none}"
  [ -z "$missing" ] || { DETAIL="${DETAIL} | ages: ${report}"; return 1; }
}

# Same data, but read through Grafana's provisioned datasource.
check_both_visible_in_grafana() {
  local dev age
  if ! AGES="$(ages_of device_uptime_seconds grafana_ds_query 2>&1)"; then
    DETAIL="Grafana datasource proxy query failed: ${AGES}"; return 1
  fi
  for dev in "$DEVICE_A" "$DEVICE_B"; do
    age="$(age_for "$dev")"
    DETAIL="${dev} age via Grafana: ${age:-none}"
    lt "$age" "$FRESH_SECONDS" || return 1
  done
}

# DEVICE_B past the offline threshold while DEVICE_A stays below it.
check_b_offline_a_online() {
  local a b
  agents_alive_a || return 2
  if ! AGES="$(ages_of device_uptime_seconds vm_query 2>&1)"; then
    DETAIL="VictoriaMetrics query failed: ${AGES}"; return 1
  fi
  a="$(age_for "$DEVICE_A")"; b="$(age_for "$DEVICE_B")"
  DETAIL="${DEVICE_A} age=${a:-none}s (must stay < ${OFFLINE_SECONDS}), ${DEVICE_B} age=${b:-none}s (must exceed ${OFFLINE_SECONDS})"
  if [ -n "$a" ] && ! lt "$a" "$OFFLINE_SECONDS"; then
    DETAIL="${DETAIL}: ${DEVICE_A} went stale although its agent is still running"; return 2
  fi
  lt "$a" "$OFFLINE_SECONDS" && gt "$b" "$OFFLINE_SECONDS"
}

agents_alive_a() {
  if ! kill -0 "$PID_A" 2>/dev/null; then
    DETAIL="agent ${DEVICE_A} (pid ${PID_A}) died; see $(agent_log "$DEVICE_A")"; return 1
  fi
}

# rule_states: fills RULE_OUT with "device_id state" lines. rc 0 ok, 3 rule missing, other = API error.
rule_states() {
  local rc=0
  RULE_OUT="$(grafana_get /api/prometheus/grafana/api/v1/rules | "$PY" -c "$PARSE_RULE_STATES" "$ALERT_NAME")" || rc=$?
  return $rc
}
state_of() { printf '%s\n' "$RULE_OUT" | awk -v d="$1" '$1 == d { print $2; exit }'; }

# Before stopping anything: the alert must not be firing for either online device.
check_alert_quiet() {
  local rc=0
  rule_states || rc=$?
  if [ "$rc" -ne 0 ]; then DETAIL="rules API returned rc=${rc}"; return 1; fi
  local sa sb
  sa="$(state_of "$DEVICE_A")"; sb="$(state_of "$DEVICE_B")"
  DETAIL="${DEVICE_A}=${sa:-none}, ${DEVICE_B}=${sb:-none}"
  [ "$sa" != alerting ] && [ "$sb" != alerting ]
}

# After stopping DEVICE_B: firing for B, never for A.
check_alert_firing_b_only() {
  local rc=0
  agents_alive_a || return 2
  rule_states || rc=$?
  if [ "$rc" -ne 0 ]; then DETAIL="rules API returned rc=${rc}"; return 1; fi
  local sa sb
  sa="$(state_of "$DEVICE_A")"; sb="$(state_of "$DEVICE_B")"
  DETAIL="${DEVICE_A}=${sa:-none} ${DEVICE_B}=${sb:-none} (waiting for ${DEVICE_B}=alerting)"
  if [ "$sa" = alerting ]; then
    DETAIL="${DETAIL}: alert fired for the still-running ${DEVICE_A}"; return 2
  fi
  [ "$sb" = alerting ]
}

check_alertmanager_b_only() {
  local out
  if ! out="$(grafana_get '/api/alertmanager/grafana/api/v2/alerts?active=true&silenced=false&inhibited=false' |
    "$PY" -c "$PARSE_AM_ALERTS" "$ALERT_NAME" 2>&1)"; then
    DETAIL="alertmanager API error: ${out}"; return 1
  fi
  DETAIL="active ${ALERT_NAME} alerts for: ${out:-nobody}"
  if printf '%s\n' "$out" | grep -qx "$DEVICE_A"; then return 2; fi
  printf '%s\n' "$out" | grep -qx "$DEVICE_B"
}

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
stop_agent() {
  local pid=$1
  [ -n "$pid" ] || return 0
  kill "$pid" 2>/dev/null || return 0
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
  done
  kill -9 "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

cleanup() {
  local code=$?
  trap - EXIT
  set +e
  stop_agent "$PID_A"
  stop_agent "$PID_B"
  if [ "$DO_DOWN" = 1 ]; then
    note "Stopping the stack (docker compose down, volumes kept)..."
    if (cd "$REPO_ROOT" && docker compose down >/dev/null 2>&1); then note "Stack stopped."; else note "docker compose down failed."; fi
  fi
  echo
  echo "================ SUMMARY ================"
  if [ "$code" -eq 0 ]; then
    printf '%sRESULT: PASS%s  (%d of %d steps passed)\n' "$C_PASS" "$C_OFF" "$STEPS_PASSED" "$STEPS_RUN"
  else
    printf '%sRESULT: FAIL%s  (%d of %d steps passed)\n' "$C_FAIL" "$C_OFF" "$STEPS_PASSED" "$STEPS_RUN"
    if [ -n "$FAILED_STEP" ]; then
      echo "Failed step: ${FAILED_STEP}"
      [ -n "$FAILED_REASON" ] && echo "Reason:      ${FAILED_REASON}"
    else
      echo "Script aborted unexpectedly (exit code ${code})."
    fi
  fi
  if [ -n "$LOG_DIR" ]; then
    echo "Agent logs:  ${LOG_DIR}"
    if [ "$code" -ne 0 ]; then
      local f
      for f in "$LOG_DIR"/*.log; do
        [ -f "$f" ] || continue
        echo "--- tail of ${f} ---"
        tail -n 8 "$f"
      done
    fi
  fi
  [ "$DO_DOWN" = 1 ] || echo "The stack was left running. Stop it with: docker compose down"
  exit "$code"
}

# start_agent DEVICE: launches the agent in the background, sets STARTED_PID.
# (Not called in a command substitution, so the process stays a child of this shell.)
start_agent() {
  "$PYTHON_BIN" "$AGENT_SCRIPT" --device-id "$1" --endpoint "$OTLP_ENDPOINT" --interval 5 \
    >"$(agent_log "$1")" 2>&1 &
  STARTED_PID=$!
  PIDS+=("$STARTED_PID")
}

main() {
  local arg rc=0
  for arg in "$@"; do
    case "$arg" in
      --down) DO_DOWN=1 ;;
      -h | --help) usage; exit 0 ;;
      *) echo "Unknown argument: ${arg} (try --help)" >&2; exit 2 ;;
    esac
  done

  cd "$REPO_ROOT"
  trap cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  # Choose Python: explicit PYTHON wins, then .venv, then python3.
  if [ -n "${PYTHON:-}" ]; then PYTHON_BIN="$PYTHON"
  elif [ -x "${REPO_ROOT}/.venv/bin/python" ]; then PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
  else PYTHON_BIN="python3"; fi
  PY="$PYTHON_BIN"

  echo "ICU observability stack: end-to-end validation"
  note "repo=${REPO_ROOT} python=${PYTHON_BIN} vm=${VM_URL} grafana=${GRAFANA_URL} otlp=${OTLP_ENDPOINT}"
  echo

  # --- 1. prerequisites ---------------------------------------------------
  local missing="" tool
  for tool in docker curl awk "$PYTHON_BIN"; do
    command -v "$tool" >/dev/null 2>&1 || [ -x "$tool" ] || missing+="${tool} "
  done
  [ -z "$missing" ] || fail "Prerequisites" "missing tools: ${missing}"
  docker compose version >/dev/null 2>&1 || fail "Prerequisites" "'docker compose' (Compose v2) is not available"
  docker info >/dev/null 2>&1 || fail "Prerequisites" "cannot talk to the Docker daemon (is it running, are you in the docker group?)"
  "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' ||
    fail "Prerequisites" "Python 3.9+ required (${PYTHON_BIN})"
  "$PYTHON_BIN" -c 'import opentelemetry.sdk.metrics, opentelemetry.exporter.otlp.proto.grpc.metric_exporter' 2>/dev/null ||
    fail "Prerequisites" "OpenTelemetry packages missing for ${PYTHON_BIN}. Run: python3 -m venv .venv && . .venv/bin/activate && pip install -r agent/requirements.txt"
  [ -f "$AGENT_SCRIPT" ] || fail "Prerequisites" "agent script not found: ${AGENT_SCRIPT}"
  pass "Prerequisites: docker, compose v2, curl, Python 3.9+ with OpenTelemetry packages, agent script"

  LOG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/icu-validate.XXXXXX")"

  # --- 2. stack up --------------------------------------------------------
  docker compose up -d >"${LOG_DIR}/compose-up.log" 2>&1 ||
    fail "docker compose up -d" "see ${LOG_DIR}/compose-up.log: $(tail -n 3 "${LOG_DIR}/compose-up.log" | tr '\n' ' ')"
  pass "docker compose up -d"

  wait_for "$STACK_TIMEOUT" 2 check_vm_health && rc=0 || rc=$?
  wait_result "VictoriaMetrics healthy (${VM_URL}/health)" "$rc" "VictoriaMetrics did not become healthy"
  wait_for "$STACK_TIMEOUT" 2 check_grafana_health && rc=0 || rc=$?
  wait_result "Grafana healthy (${GRAFANA_URL}/api/health)" "$rc" "Grafana did not become healthy"
  wait_for "$STACK_TIMEOUT" 2 check_otlp_port && rc=0 || rc=$?
  wait_result "OTLP port reachable (${OTLP_ENDPOINT})" "$rc" "nothing is listening on the OTLP gRPC port"

  # --- 3. Grafana provisioning (fail fast) --------------------------------
  if [ "$SKIP_GRAFANA" != 1 ]; then
    local ds
    ds="$(grafana_get "/api/datasources/uid/${DS_UID}" 2>&1)" ||
      fail "Grafana datasource VictoriaMetrics provisioned" "GET /api/datasources/uid/${DS_UID} failed: ${ds}"
    printf '%s' "$ds" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("type")=="prometheus" and d.get("name")=="VictoriaMetrics" else 1)' ||
      fail "Grafana datasource VictoriaMetrics provisioned" "datasource uid ${DS_UID} exists but is not type prometheus named VictoriaMetrics"
    pass "Grafana datasource VictoriaMetrics provisioned (uid ${DS_UID})"

    local dash
    dash="$(grafana_get "/api/dashboards/uid/${DASHBOARD_UID}" 2>&1)" ||
      fail "Grafana dashboard Device Fleet provisioned" "GET /api/dashboards/uid/${DASHBOARD_UID} failed: ${dash}"
    printf '%s' "$dash" | "$PY" -c 'import json,sys; sys.exit(0 if json.load(sys.stdin)["dashboard"]["title"]=="Device Fleet" else 1)' ||
      fail "Grafana dashboard Device Fleet provisioned" "dashboard ${DASHBOARD_UID} exists but its title is not 'Device Fleet'"
    pass "Grafana dashboard 'Device Fleet' provisioned (uid ${DASHBOARD_UID})"

    local rc2=0
    rule_states || rc2=$?
    [ "$rc2" -eq 0 ] ||
      fail "Grafana alert rule ${ALERT_NAME} provisioned" "rule not found via /api/prometheus/grafana/api/v1/rules (rc=${rc2}). Check: docker compose logs grafana | grep -i provision"
    pass "Grafana alert rule ${ALERT_NAME} provisioned"
  else
    note "SKIP_GRAFANA=1: skipping Grafana provisioning and alert checks"
  fi

  # --- 4. no leftover devices with the same ids --------------------------
  local stale
  if ! AGES="$(ages_of device_uptime_seconds vm_query 2>&1)"; then
    fail "Pre-check: ${DEVICE_A} and ${DEVICE_B} are not already reporting" "VictoriaMetrics query failed: ${AGES}"
  fi
  stale=""
  for tool in "$DEVICE_A" "$DEVICE_B"; do
    lt "$(age_for "$tool")" "$FRESH_SECONDS" && stale+="${tool} "
  done
  [ -z "$stale" ] ||
    fail "Pre-check: ${DEVICE_A} and ${DEVICE_B} are not already reporting" "${stale}already pushing data from somewhere else. Stop those agents, wait ~40 s, rerun."
  pass "Pre-check: ${DEVICE_A} and ${DEVICE_B} are not already reporting"

  # --- 5. start two agents ------------------------------------------------
  start_agent "$DEVICE_A"; PID_A=$STARTED_PID
  start_agent "$DEVICE_B"; PID_B=$STARTED_PID
  sleep 1
  agents_alive || fail "Start two agents" "${DETAIL}. Log: $(tail -n 5 "$(agent_log "$DEVICE_A")" "$(agent_log "$DEVICE_B")" 2>/dev/null | tr '\n' ' ')"
  pass "Started agents ${DEVICE_A} (pid ${PID_A}) and ${DEVICE_B} (pid ${PID_B})"

  # --- 6. both devices fresh, all four always-present metrics -------------
  wait_for "$ONLINE_TIMEOUT" 3 check_both_fresh && rc=0 || rc=$?
  wait_result "Both devices report fresh (age < ${FRESH_SECONDS}s) cpu, memory, disk, uptime and report-interval metrics" "$rc" "metrics did not arrive in VictoriaMetrics"

  # --- 7. distinct identities --------------------------------------------
  local ident
  ident="$(vm_query "last_over_time(device_uptime_seconds{device_id=~\"${DEVICE_A}|${DEVICE_B}\"}[1m])" 2>&1 |
    "$PY" -c "$PARSE_IDENTITY" "$DEVICE_A" "$DEVICE_B" 2>&1)" ||
    fail "Distinct device identities (one series each; instance=device_id; job=device-agent)" "$ident"
  pass "Distinct device identities (one series each; instance=device_id; job=device-agent)"

  if [ "$SKIP_GRAFANA" != 1 ]; then
    wait_for 30 3 check_both_visible_in_grafana && rc=0 || rc=$?
    wait_result "Both devices visible through Grafana's VictoriaMetrics datasource" "$rc" "Grafana cannot read the device series"

    wait_for 90 5 check_alert_quiet && rc=0 || rc=$?
    wait_result "Baseline: ${ALERT_NAME} not firing while both devices are online" "$rc" "${ALERT_NAME} still firing for an online device"
  fi

  # --- 8. stop device B ---------------------------------------------------
  stop_agent "$PID_B"
  PIDS=("$PID_A")
  pass "Stopped agent ${DEVICE_B} (simulated device outage)"
  local t_stop=$SECONDS

  wait_for "$OFFLINE_TIMEOUT" 3 check_b_offline_a_online && rc=0 || rc=$?
  wait_result "${DEVICE_B} age > ${OFFLINE_SECONDS}s (OFFLINE) while ${DEVICE_A} stays < ${OFFLINE_SECONDS}s (ONLINE)" "$rc" "${DEVICE_B} never crossed the offline threshold"
  note "      offline detected $((SECONDS - t_stop))s after stopping the agent"

  # --- 9. alert -----------------------------------------------------------
  if [ "$SKIP_GRAFANA" != 1 ]; then
    wait_for "$ALERT_TIMEOUT" 5 check_alert_firing_b_only && rc=0 || rc=$?
    wait_result "Grafana alert ${ALERT_NAME} firing for ${DEVICE_B} and not for ${DEVICE_A}" "$rc" "alert never reached the firing state"
    note "      alert firing $((SECONDS - t_stop))s after stopping the agent"

    wait_for 60 5 check_alertmanager_b_only && rc=0 || rc=$?
    wait_result "Grafana Alertmanager lists an active ${ALERT_NAME} for ${DEVICE_B} only" "$rc" "alert did not reach the Alertmanager"
  fi

  # Cleanup (agents, optional compose down) and the summary happen in the EXIT trap.
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
