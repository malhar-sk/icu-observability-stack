#!/usr/bin/env bash
# End-to-end check of `./fleet watch`: real containers, a real exporter, real stop/start, the real alert.
#
#   scripts/validate_watch.sh [--keep] [--help]
#
# Starts the stack (./fleet up), starts a small victim web server on the compose network, and proves:
#   - a ping, a TCP port, a web page, a page that must answer 404, and two metrics endpoints (an ESPHome-style one and
#     VictoriaMetrics' own, a real exporter) all appear as ONLINE devices, reported "via watcher"
#   - the stored labels are exactly what CONTRACT.md promises, and scraped numbers arrive under their own names
#   - a hostile metrics page cannot overwrite our series, cannot add NaN, and is cut at the series limit (2000 by default)
#   - a web page given as a metrics target, and a host name that does not exist, are refused with the real reason
#   - stopping the victim makes its devices OFFLINE after 30 s and fires DeviceOffline; starting it brings them back
#     with a restarted uptime; a watcher restart keeps watching
#   - `watch remove` deletes one target and its data; plain `remove` refuses a watched one
#
# Needs: Docker with Compose v2, python3, curl. Takes about 6 minutes. Exits 0 only if every step passes.
# Never runs `docker compose down -v`. The test targets (vt-*) are removed at the end unless --keep is given.
#
# Environment overrides (defaults in brackets): VM_URL [http://localhost:8428]  GRAFANA_URL [http://localhost:3000]
#   GRAFANA_AUTH [admin:admin]

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VM_URL="${VM_URL:-http://localhost:8428}"
GRAFANA_URL="${GRAFANA_URL:-http://localhost:3000}"
GRAFANA_AUTH="${GRAFANA_AUTH:-admin:admin}"
NETWORK="$(basename "$ROOT")_default"
VICTIM=vt-victim
KEEP=0
case "${1-}" in
  --keep) KEEP=1 ;;
  -h|--help) sed -n '2,/^set -uo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
  "") ;;
  *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
esac

if [ -t 1 ]; then C_PASS=$'\033[32m'; C_FAIL=$'\033[31m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'; else C_PASS=""; C_FAIL=""; C_DIM=""; C_OFF=""; fi
RUN=0; OK=0; FAILED=0
ids=(vt-ping vt-tcp vt-http vt-404 vt-esp vt-vm vt-hostile vt-small)

pass() { RUN=$((RUN + 1)); OK=$((OK + 1)); printf '%s PASS%s  %s %s%s%s\n' "$C_PASS" "$C_OFF" "$1" "$C_DIM" "${2-}" "$C_OFF"; }
fail() { RUN=$((RUN + 1)); FAILED=1; printf '%s FAIL%s  %s %s\n' "$C_FAIL" "$C_OFF" "$1" "${2-}"; }
check() { local name=$1; shift; if "$@"; then pass "$name"; else fail "$name"; fi; }
fleet() { "$ROOT/fleet" "$@"; }
now() { date +%s; }
has() { grep -qi -- "$2" <<<"$1"; }  # has <text> <pattern>

state_of() {  # state_of <id> -> ONLINE / OFFLINE / - (not known)
  fleet list --json 2>/dev/null | python3 -c '
import json, sys
for d in json.load(sys.stdin):
    if d["device_id"] == sys.argv[1]:
        print(d["state"]); break
else:
    print("-")' "$1"
}
field_of() {  # field_of <id> <field>
  fleet list --json 2>/dev/null | python3 -c '
import json, sys
for d in json.load(sys.stdin):
    if d["device_id"] == sys.argv[1]:
        print(d[sys.argv[2]]); break' "$1" "$2"
}
query() {  # query <promql> -> "<labels json> <value>" per series
  curl -s -G "$VM_URL/api/v1/query" --data-urlencode "query=$1" | python3 -c '
import json, sys
for r in json.load(sys.stdin)["data"]["result"]:
    print(json.dumps(r["metric"], sort_keys=True), r["value"][1])'
}
scalar() { query "$1" | head -1 | awk '{print $NF}'; }
alert_json() {  # alert_json <id> -> "state|summary" of the DeviceOffline alert for it, or "-"
  curl -s -u "$GRAFANA_AUTH" "$GRAFANA_URL/api/prometheus/grafana/api/v1/rules" | python3 -c '
import json, sys
for g in json.load(sys.stdin)["data"]["groups"]:
    for rule in g["rules"]:
        if rule["name"] == "DeviceOffline":
            for a in rule.get("alerts") or []:
                if a["labels"].get("device_id") == sys.argv[1]:
                    print(a["state"] + "|" + a["annotations"].get("summary", "")); sys.exit()
print("-")' "$1"
}
is_state() { [ "$(state_of "$1")" = "$2" ]; }
alerting() { [[ "$(alert_json "$1")" == Alerting* ]]; }
wait_until() {  # wait_until <seconds> <function> [args]: runs it again every second
  local limit=$1 start; shift; start=$(now)
  while ! "$@" >/dev/null 2>&1; do [ $(($(now) - start)) -ge "$limit" ] && return 1; sleep 1; done
  return 0
}
refused_with() { [ "$1" = 1 ] && has "$2" "$3"; }  # refused_with <exit code> <output> <pattern>
watcher_running() { docker compose ps --format '{{.Service}} {{.State}}' | grep -q 'watcher running'; }
said_once() { [ "$(grep -c -- "$2" <<<"$1")" = 1 ]; }  # said_once <log> <pattern>
all_online() { local id; for id in "$@"; do is_state "$id" ONLINE || return 1; done; }
all_offline() { local id; for id in "$@"; do is_state "$id" OFFLINE || return 1; done; }

cleanup() {
  docker rm -f "$VICTIM" >/dev/null 2>&1
  if [ "$KEEP" = 0 ]; then  # faster than `watch remove` for each: the explicit remove is tested in the script itself
    python3 - "$ROOT/watch/targets.json" <<'PY' 2>/dev/null
import json, sys
path = sys.argv[1]
doc = json.load(open(path))
doc["targets"] = [t for t in doc["targets"] if not t["id"].startswith("vt-")]
json.dump(doc, open(path, "w"), indent=2)
PY
    sleep 4
    curl -s -X POST "$VM_URL/api/v1/admin/tsdb/delete_series" --data-urlencode 'match[]={device_id=~"vt-.*"}' >/dev/null
    curl -s -X POST "$VM_URL/internal/resetRollupResultCache" >/dev/null
  fi
}
trap cleanup EXIT

cd "$ROOT" || exit 2
printf 'Watcher end-to-end check (about 6 minutes)\n\n'

# ---- 1. the stack and a victim to watch
run_start=$(date -u +%Y-%m-%dT%H:%M:%SZ)  # the watcher's log from before this run must not count (and `fleet up` restarts it)
if fleet up >/tmp/validate_watch_up.log 2>&1; then pass "./fleet up"; else fail "./fleet up" "see /tmp/validate_watch_up.log"; cat /tmp/validate_watch_up.log; exit 1; fi
docker rm -f "$VICTIM" >/dev/null 2>&1
docker run -d --name "$VICTIM" --network "$NETWORK" -v "$ROOT/tests/fixtures/metrics_server.py:/srv/server.py:ro" \
  python:3.13-alpine python /srv/server.py 8000 >/dev/null 2>&1
started_ok() { docker exec "$VICTIM" wget -qO- http://localhost:8000/ >/dev/null 2>&1; }
check "the victim web server is up" wait_until 20 started_ok

# ---- 2. add the targets: one with the CLI waiting for it, the rest in one go
t0=$(now)
out=$(fleet watch add vt-ping ping "$VICTIM" --interval 5 2>&1); rc=$?
if [ "$rc" = 0 ]; then pass "watch add (ping) waits until it answers" "after $(( $(now) - t0 )) s, including starting the watcher"; else fail "watch add (ping)" "$out"; fi
check "the watcher is running" watcher_running
fleet watch add vt-tcp tcp "$VICTIM:8000" --interval 5 --no-wait >/dev/null 2>&1
fleet watch add vt-http http "http://$VICTIM:8000/" --interval 5 --no-wait >/dev/null 2>&1
fleet watch add vt-404 http "http://$VICTIM:8000/nothing" --expect-status 404 --interval 5 --no-wait >/dev/null 2>&1
fleet watch add vt-esp metrics "http://$VICTIM:8000/metrics" --interval 5 --no-wait >/dev/null 2>&1
fleet watch add vt-vm metrics "http://victoriametrics:8428/metrics" --interval 5 --no-wait >/dev/null 2>&1
fleet watch add vt-hostile metrics "http://$VICTIM:8000/hostile" --interval 5 --no-wait >/dev/null 2>&1
fleet watch add vt-small metrics "http://victoriametrics:8428/metrics" --interval 5 --max-series 100 --no-wait >/dev/null 2>&1
start=$(now)
for id in "${ids[@]}"; do
  if wait_until 60 is_state "$id" ONLINE; then pass "$id appears ONLINE" "after $(( $(now) - start )) s"; else fail "$id appears ONLINE" "state: $(state_of "$id")"; fi
done
for id in "${ids[@]}"; do check "$id is reported via watcher" test "$(field_of "$id" via)" = watcher; done

# ---- 3. what is stored
labels=$(query 'device_uptime_seconds{device_id="vt-ping"}' | head -1 | python3 -c '
import json, sys
m = json.loads(sys.stdin.read().rsplit(" ", 1)[0]); print(",".join(sorted(m)), m.get("job"), m.get("kind"))')
check "stored labels are exactly device_id, job=watcher and kind" test "$labels" = "__name__,device_id,job,kind watcher ping"
check "the probe is recorded (success 1)" test "$(scalar 'device_probe_success{device_id="vt-ping"}')" = 1
check "the interval is declared (5 s)" test "$(scalar 'device_report_interval_seconds{device_id="vt-ping"}')" = 5
check "the HTTP status is recorded (200, and 404 for the one that expects it)" test \
  "$(scalar 'device_http_status_code{device_id="vt-http"}')/$(scalar 'device_http_status_code{device_id="vt-404"}')" = 200/404
check "a tcp target has no HTTP status" test "$(query 'device_http_status_code{device_id="vt-tcp"}' | wc -l)" = 0
check "a scraped number arrives under its own name (esphome temperature 21.5)" test "$(scalar 'esphome_sensor_value{device_id="vt-esp",id="temperature"}')" = 21.5
check "the scrape size is recorded (4 series)" test "$(scalar 'device_scrape_series{device_id="vt-esp"}')" = 4
whole() { [ "$(scalar 'count({device_id="vt-vm"})')" -ge 1100 ] 2>/dev/null; }  # its page has about 1200 series
check "a real exporter (VictoriaMetrics' own /metrics, about 1200 series) is stored whole under the default limit" wait_until 30 whole
check "--max-series keeps only that many (100) of the same page" test "$(scalar 'count({device_id="vt-small",__name__!~"device_.*"})')" = 100

# ---- 4. a hostile page
uptime=$(scalar 'device_uptime_seconds{device_id="vt-hostile"}')
check "a scraped device_uptime_seconds cannot overwrite ours (value stays small, not 999999)" test "${uptime%.*}" -lt 300
check "a scraped device_probe_success cannot overwrite ours (still 1)" test "$(scalar 'device_probe_success{device_id="vt-hostile"}')" = 1
check "NaN and infinity are not stored" test "$(query '{device_id="vt-hostile",__name__=~"not_a_number|infinite"}' | wc -l)" = 0
check "the page is cut at the default limit (2500 offered, 2000 kept)" test "$(scalar 'count({device_id="vt-hostile",__name__=~"hostile_metric_.*"})')" = 2000
logs=$(docker compose logs --no-log-prefix --since "$run_start" watcher 2>&1)
check "the watcher said so about the reserved names, once" said_once "$logs" "vt-hostile: metric names starting with device_"
check "...and about the series limit, once" said_once "$logs" "vt-hostile: more than 2000 series"
check "a page under its limit gets no warning" test "$(grep -c 'vt-vm: more than' <<<"$logs")" = 0

# ---- 5. refusals that say why
out=$(fleet watch add vt-page metrics "http://$VICTIM:8000/" --interval 5 --wait 40 2>&1); rc=$?
check "a web page is not accepted as a metrics endpoint (and says so)" refused_with "$rc" "$out" "not Prometheus text"
out=$(fleet watch add vt-ghost ping no-such-host.invalid --interval 5 --wait 40 2>&1); rc=$?
check "a host name that does not exist is explained" refused_with "$rc" "$out" "cannot resolve"
check "neither is on the dashboard" test "$(state_of vt-page)$(state_of vt-ghost)" = "--"
listing=$(fleet watch list 2>&1)
check "'watch list' shows them as waiting" has "$listing" "vt-ghost.*waiting"
ids+=(vt-page vt-ghost)

# ---- 6. the victim goes away
online_ids=(vt-ping vt-tcp vt-http vt-404 vt-esp vt-vm vt-hostile)
docker stop "$VICTIM" >/dev/null 2>&1; stopped=$(now)
if wait_until 90 all_offline vt-ping vt-tcp vt-http vt-404 vt-esp vt-hostile; then pass "everything on the victim goes OFFLINE" "after $(( $(now) - stopped )) s (rule: 30 s for a 5 s interval)"; else fail "everything on the victim goes OFFLINE" "$(for i in "${online_ids[@]}"; do printf '%s=%s ' "$i" "$(state_of "$i")"; done)"; fi
check "the target on another container (victoriametrics) stays ONLINE" is_state vt-vm ONLINE
if wait_until 150 alerting vt-ping; then pass "DeviceOffline fires for the pinged target" "$(( $(now) - stopped )) s after it went away"; else fail "DeviceOffline fires for vt-ping" "alert: $(alert_json vt-ping)"; fi
summary=$(alert_json vt-ping)
check "the alert text names the device and the limit" has "$summary" "vt-ping has not reported for .* offline after 30 s"
not_alerting() { ! alerting "$1"; }
check "no alert for the target that is still answering" not_alerting vt-vm
failing=$(scalar 'device_probe_success{device_id="vt-ping"}')
check "the failure is recorded (probe success 0)" test "$failing" = 0
check "no uptime is written while it is down (the last sample is old)" test "$(scalar 'time() - tlast_over_time(device_uptime_seconds{device_id="vt-ping"}[1d])' | cut -d. -f1)" -ge 25

# ---- 7. it comes back
docker start "$VICTIM" >/dev/null 2>&1; restarted=$(now)
if wait_until 60 all_online vt-ping vt-tcp vt-http vt-404 vt-esp vt-hostile; then pass "everything is ONLINE again" "after $(( $(now) - restarted )) s"; else fail "everything is ONLINE again" "$(for i in "${online_ids[@]}"; do printf '%s=%s ' "$i" "$(state_of "$i")"; done)"; fi
sleep 6
check "its uptime started over (counts from the new answer, under 60 s)" test "$(field_of vt-ping uptime | cut -d. -f1)" -lt 60
check "the alert clears" wait_until 90 not_alerting vt-ping

# ---- 8. the watcher itself
before=$(docker compose ps -q watcher)
fleet watch add vt-late tcp "$VICTIM:8000" --interval 5 --no-wait >/dev/null 2>&1; ids+=(vt-late)
check "adding a target does not restart the watcher" test "$before" = "$(docker compose ps -q watcher)"
check "...and the new target is picked up" wait_until 40 is_state vt-late ONLINE
docker compose restart watcher >/dev/null 2>&1
check "after a watcher restart it keeps watching the same targets" wait_until 60 all_online vt-ping vt-tcp vt-http vt-late
check "'watch list' still lists them" has "$(fleet watch list 2>&1)" "vt-late.*ONLINE"
check "'fleet check' says how it is watched" has "$(fleet check vt-ping 2>&1)" "via *watcher (ping $VICTIM)"

# ---- 9. removing
out=$(fleet remove vt-tcp --yes 2>&1); rc=$?
check "plain 'remove' refuses a watched id and names the right command" refused_with "$rc" "$out" "fleet watch remove vt-tcp"
out=$(fleet watch remove vt-tcp --yes 2>&1); rc=$?
check "'watch remove' stops watching" test "$rc" = 0
check "...its data is gone" test "$(query 'count({device_id="vt-tcp"})' | wc -l)" = 0
check "...and it is gone from the device list" test "$(state_of vt-tcp)" = "-"
check "...while the others are untouched" is_state vt-ping ONLINE

printf '\n%d/%d steps passed\n' "$OK" "$RUN"
[ "$FAILED" = 0 ] && [ "$OK" = "$RUN" ]
