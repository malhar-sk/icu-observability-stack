#!/usr/bin/env bash
# End-to-end check of the MQTT input: a real broker, the real bridge, simulated devices, the real alert.
#
#   scripts/validate_mqtt.sh [--keep] [--help]
#
# Runs `./fleet up --mqtt` (safe to repeat), then proves:
#   - the broker refuses a wrong password and anonymous clients
#   - three simulated boards appear as ONLINE devices, reported "via MQTT", with only the labels we promise
#   - a reading with a made-up name (humidity_percent) is accepted; bad names and bad payloads are not
#   - a board that declares a 60 s report interval is still ONLINE after 45 s of silence (the old 30 s rule would flag it)
#   - a board that stops reporting goes OFFLINE after 30 s, and the DeviceOffline alert fires for it alone
#   - restarting the bridge does not bring a dead board back to life (retained messages are not fresh data)
#
# Needs: Docker with Compose v2, python3, curl. Takes about 4 minutes. Exits 0 only if every step passes.
# Never runs `docker compose down -v`. The simulated boards (test-m-*) are removed at the end unless --keep is given.
#
# Environment overrides (defaults in brackets): VM_URL [http://localhost:8428]  GRAFANA_URL [http://localhost:3000]
#   GRAFANA_AUTH [admin:<the password in .env, else admin>]

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VM_URL="${VM_URL:-http://localhost:8428}"
GRAFANA_URL="${GRAFANA_URL:-http://localhost:3000}"
# Grafana's login: GRAFANA_AUTH if given, else the password ./fleet keeps in .env, else Grafana's default.
grafana_pw() { local p; p=$(sed -n 's/^GRAFANA_ADMIN_PASSWORD=//p' "$ROOT/.env" 2>/dev/null | head -1 | tr -d "'"); echo "${p:-admin}"; }
USER_AUTH="${GRAFANA_AUTH:-}"; GRAFANA_AUTH="${USER_AUTH:-admin:$(grafana_pw)}"
refresh_auth() { [ -n "$USER_AUTH" ] || GRAFANA_AUTH="admin:$(grafana_pw)"; }  # a first `./fleet up` creates the password
KEEP=0
case "${1-}" in
  --keep) KEEP=1 ;;
  -h|--help) sed -n '2,/^set -uo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
  "") ;;
  *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
esac

if [ -t 1 ]; then C_PASS=$'\033[32m'; C_FAIL=$'\033[31m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'; else C_PASS=""; C_FAIL=""; C_DIM=""; C_OFF=""; fi
RUN=0; OK=0; FAILED=0; PIDS=(); PW=""
ids=(test-m-a test-m-b test-m-slow)

pass() { RUN=$((RUN + 1)); OK=$((OK + 1)); printf '%s PASS%s  %s %s%s%s\n' "$C_PASS" "$C_OFF" "$1" "$C_DIM" "${2-}" "$C_OFF"; }
fail() { RUN=$((RUN + 1)); FAILED=1; printf '%s FAIL%s  %s %s\n' "$C_FAIL" "$C_OFF" "$1" "${2-}"; }
check() { local name=$1; shift; if "$@"; then pass "$name"; else fail "$name"; fi; }  # a condition without detail
fleet() { "$ROOT/fleet" "$@"; }
pub() { docker compose exec -T mosquitto mosquitto_pub -h localhost -u fleet -P "$PW" "$@"; }
now() { date +%s; }
has() { grep -qi -- "$2" <<<"$1"; }  # has <text> <pattern>

state_of() {  # state_of <id> -> ONLINE / OFFLINE / - (unknown)
  fleet list --json 2>/dev/null | python3 -c '
import json, sys
want = sys.argv[1]
for d in json.load(sys.stdin):
    if d["device_id"] == want:
        print(d["state"]); break
else:
    print("-")' "$1"
}
field_of() {  # field_of <id> <field> -> the value from `fleet list --json`
  fleet list --json 2>/dev/null | python3 -c '
import json, sys
for d in json.load(sys.stdin):
    if d["device_id"] == sys.argv[1]:
        print(d[sys.argv[2]]); break' "$1" "$2"
}
query() {  # query <promql> -> one line per result series: "<labels json> <value>"
  curl -s -G "$VM_URL/api/v1/query" --data-urlencode "query=$1" | python3 -c '
import json, sys
for r in json.load(sys.stdin)["data"]["result"]:
    print(json.dumps(r["metric"], sort_keys=True), r["value"][1])'
}
alert_state() {  # alert_state <id> -> Alerting / Pending / Normal / - for the DeviceOffline alert
  curl -s -u "$GRAFANA_AUTH" "$GRAFANA_URL/api/prometheus/grafana/api/v1/rules" | python3 -c '
import json, sys
want = sys.argv[1]
for g in json.load(sys.stdin)["data"]["groups"]:
    for rule in g["rules"]:
        if rule["name"] == "DeviceOffline":
            for a in rule.get("alerts") or []:
                if a["labels"].get("device_id") == want:
                    print(a["state"]); sys.exit()
print("-")' "$1"
}
is_state() { [ "$(state_of "$1")" = "$2" ]; }                  # is_state <id> ONLINE|OFFLINE
alerting() { [ "$(alert_state "$1")" = Alerting ]; }
not_alerting() { ! alerting "$1"; }
bridge_connected() { docker compose logs --no-log-prefix --tail 5 mqtt-bridge 2>&1 | grep -q "connected to"; }
wait_until() {  # wait_until <seconds> <command...>: runs the command again every second, so give it a function
  local limit=$1 start; shift; start=$(now)
  while ! "$@" >/dev/null 2>&1; do [ $(($(now) - start)) -ge "$limit" ] && return 1; sleep 1; done
  return 0
}

device() {  # device <id> <declared interval or -> <seconds between reports>: a pretend board, runs until killed
  local id=$1 declared=$2 every=$3 n=0
  [ "$declared" != "-" ] && pub -t "fleet/$id/report_interval_seconds" -m "$declared" -r
  while :; do
    n=$((n + every))
    pub -t "fleet/$id/uptime_seconds" -m "$n"
    pub -t "fleet/$id/temperature_celsius" -m 21.5
    pub -t "fleet/$id/humidity_percent" -m 48
    [ "$every" = 0 ] && return 0  # a board that reports once
    sleep "$every"
  done
}

cleanup() {
  [ "${#PIDS[@]}" -gt 0 ] && kill "${PIDS[@]}" 2>/dev/null
  if [ "$KEEP" = 0 ]; then
    for id in "${ids[@]}"; do fleet remove "$id" --force --yes >/dev/null 2>&1; done
    for id in test-m-a test-m-slow; do pub -t "fleet/$id/report_interval_seconds" -r -n 2>/dev/null; done  # clear what they left retained
  fi
}
trap cleanup EXIT

cd "$ROOT" || exit 2
printf 'MQTT end-to-end check (about 4 minutes)\n\n'

# ---- 1. the stack, with MQTT
if fleet up --mqtt >/tmp/validate_mqtt_up.log 2>&1; then refresh_auth; pass "./fleet up --mqtt" "stack and MQTT input are up"; else fail "./fleet up --mqtt" "see /tmp/validate_mqtt_up.log"; cat /tmp/validate_mqtt_up.log; exit 1; fi
PW=$(sed -n 's/^MQTT_PASSWORD=//p' .env)
[ -n "$PW" ] || { fail "password in .env" "missing"; exit 1; }
status=$(fleet status 2>&1)
healthy() { has "$status" "mosquitto.*running" && has "$status" "mqtt-bridge.*connected"; }
check "./fleet status lists the broker and the bridge as healthy" healthy

# ---- 2. access control
out=$(docker compose exec -T mosquitto mosquitto_pub -h localhost -u fleet -P wrong-password -t fleet/x/uptime_seconds -m 1 2>&1)
check "a wrong password is refused" has "$out" "not authori"
out=$(docker compose exec -T mosquitto mosquitto_pub -h localhost -t fleet/x/uptime_seconds -m 1 2>&1)
check "an anonymous client is refused" has "$out" "not authori"

# ---- 3. three boards: a (declares 10 s, reports every 5 s), b (declares nothing), slow (declares 60 s, reports once)
started=$(now)
device test-m-a 10 5 >/dev/null 2>&1 & PIDS+=($!)
device test-m-b - 5 >/dev/null 2>&1 & PIDS+=($!)
device test-m-slow 60 0 >/dev/null 2>&1
slow_reported=$(now)
for id in "${ids[@]}"; do
  if wait_until 60 is_state "$id" ONLINE; then pass "$id appears ONLINE" "after $(( $(now) - started )) s"; else fail "$id appears ONLINE" "state: $(state_of "$id")"; fi
done
for id in "${ids[@]}"; do check "$id is reported via MQTT" test "$(field_of "$id" via)" = mqtt; done

# ---- 4. what is stored
labels=$(query 'device_uptime_seconds{device_id="test-m-a"}' | head -1 | python3 -c '
import json, sys
m = json.loads(sys.stdin.read().rsplit(" ", 1)[0]); print(",".join(sorted(m)), m.get("job"))')
check "stored labels are exactly device_id and job=mqtt-bridge" test "$labels" = "__name__,device_id,job mqtt-bridge"
check "temperature is stored (21.5)" test "$(query 'device_temperature_celsius{device_id="test-m-a"}' | awk '{print $NF}')" = 21.5
check "a reading with a made-up name is accepted (humidity_percent)" test "$(query 'device_humidity_percent{device_id="test-m-a"}' | awk '{print $NF}')" = 48
check "the declared interval is stored (a: 10, slow: 60, b: none)" test \
  "$(query 'device_report_interval_seconds{device_id="test-m-a"}' | awk '{print $NF}')/$(query 'device_report_interval_seconds{device_id="test-m-slow"}' | awk '{print $NF}')/$(query 'device_report_interval_seconds{device_id="test-m-b"}' | wc -l)" = 10/60/0
pub -t fleet/test-m-a/bad_reading -m hot
pub -t fleet/test-m-a/Bad_Name -m 5
pub -t fleet/test-m-a/with-dash -m 5
pub -t fleet/test-m-a/nan_reading -m nan
sleep 8
check "bad payloads and bad names create nothing" test "$(query '{__name__=~"device_(bad_reading|Bad_Name|with-dash|nan_reading)"}' | wc -l)" = 0

# ---- 5. the slow board: 45 s after its only report it must still be ONLINE (limit 3 x 60 = 180 s)
while [ $(($(now) - slow_reported)) -lt 45 ]; do sleep 1; done
check "a board that declared 60 s is still ONLINE after 45 s of silence" is_state test-m-slow ONLINE
check "the board that declared nothing and keeps reporting is ONLINE" is_state test-m-b ONLINE

# ---- 6. b stops: OFFLINE after 30 s, alert for b only
kill "${PIDS[1]}" 2>/dev/null; stopped=$(now)
if wait_until 90 is_state test-m-b OFFLINE; then pass "a board that stops reporting goes OFFLINE" "after $(( $(now) - stopped )) s of silence (rule: 30 s)"; else fail "b goes OFFLINE" "state: $(state_of test-m-b)"; fi
if wait_until 150 alerting test-m-b; then pass "DeviceOffline fires for it" "$(( $(now) - stopped )) s after the last report"; else fail "DeviceOffline fires for test-m-b" "alert: $(alert_state test-m-b)"; fi
check "no alert for the board that keeps reporting" not_alerting test-m-a
check "no alert for the slow board (still inside its own limit)" not_alerting test-m-slow

# ---- 7. a restart of the bridge must not revive b
before=$(query 'tlast_over_time(device_uptime_seconds{device_id="test-m-b"}[1d])' | awk '{print $NF}')
docker compose restart mqtt-bridge >/dev/null 2>&1
wait_until 30 bridge_connected
sleep 15
after=$(query 'tlast_over_time(device_uptime_seconds{device_id="test-m-b"}[1d])' | awk '{print $NF}')
unrevived() { [ -n "$before" ] && [ "$before" = "$after" ] && is_state test-m-b OFFLINE; }
check "restarting the bridge does not revive a dead board" unrevived
check "...and the live board is still ONLINE afterwards" is_state test-m-a ONLINE

printf '\n%d/%d steps passed\n' "$OK" "$RUN"
[ "$FAILED" = 0 ] && [ "$OK" = "$RUN" ]
