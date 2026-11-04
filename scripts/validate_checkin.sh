#!/usr/bin/env bash
# End-to-end check of the check-in URL: the real container and the real `curl`.
#
#   scripts/validate_checkin.sh [--keep] [--help]
#
# Runs `./fleet add <id> --checkin` (safe to repeat) and proves:
#   - a wrong key and bad input are refused with a reason, and nothing is stored for them
#   - a curl check-in makes the device appear ONLINE "via checkin", with only the labels we promise, and readings stored
#   - a device that declared every=60 is still ONLINE 45 s after its only check-in (the 30 s rule would flag it)
#   - one that declared nothing goes OFFLINE after 30 s, the DeviceOffline alert fires for it, and a new check-in brings it back
#   - the key is not written to the service's log
#
# Needs: Docker with Compose v2, python3, curl. Takes about 3 minutes. Exits 0 only if every step passes.
# Never runs `docker compose down -v`. The test devices (ck-*) are removed at the end unless --keep is given.
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
RUN=0; OK=0; FAILED=0; KEY=""; BASE=""

pass() { RUN=$((RUN + 1)); OK=$((OK + 1)); printf '%s PASS%s  %s %s%s%s\n' "$C_PASS" "$C_OFF" "$1" "$C_DIM" "${2-}" "$C_OFF"; }
fail() { RUN=$((RUN + 1)); FAILED=1; printf '%s FAIL%s  %s %s\n' "$C_FAIL" "$C_OFF" "$1" "${2-}"; }
check() { local name=$1; shift; if "$@"; then pass "$name"; else fail "$name"; fi; }
fleet() { "$ROOT/fleet" "$@"; }
now() { date +%s; }
has() { grep -qi -- "$2" <<<"$1"; }

state_of() {
  fleet list --json 2>/dev/null | python3 -c '
import json, sys
for d in json.load(sys.stdin):
    if d["device_id"] == sys.argv[1]:
        print(d["state"]); break
else:
    print("-")' "$1"
}
field_of() {
  fleet list --json 2>/dev/null | python3 -c '
import json, sys
for d in json.load(sys.stdin):
    if d["device_id"] == sys.argv[1]:
        print(d[sys.argv[2]]); break' "$1" "$2"
}
query() {
  curl -s -G "$VM_URL/api/v1/query" --data-urlencode "query=$1" | python3 -c '
import json, sys
for r in json.load(sys.stdin)["data"]["result"]:
    print(json.dumps(r["metric"], sort_keys=True), r["value"][1])'
}
scalar() { query "$1" | head -1 | awk '{print $NF}'; }
alert_state() {
  curl -s -u "$GRAFANA_AUTH" "$GRAFANA_URL/api/prometheus/grafana/api/v1/rules" | python3 -c '
import json, sys
for g in json.load(sys.stdin)["data"]["groups"]:
    for rule in g["rules"]:
        if rule["name"] == "DeviceOffline":
            for a in rule.get("alerts") or []:
                if a["labels"].get("device_id") == sys.argv[1]:
                    print(a["state"]); sys.exit()
print("-")' "$1"
}
is_state() { [ "$(state_of "$1")" = "$2" ]; }
alerting() { [ "$(alert_state "$1")" = Alerting ]; }
not_alerting() { ! alerting "$1"; }
code_of() { curl -s -o /dev/null -w '%{http_code}' -m 10 "$1"; }  # code_of <url> -> the HTTP status
wait_until() {
  local limit=$1 start; shift; start=$(now)
  while ! "$@" >/dev/null 2>&1; do [ $(($(now) - start)) -ge "$limit" ] && return 1; sleep 1; done
  return 0
}
checkin() { curl -fsS -m 10 "$BASE/checkin/$1?key=$KEY&$2" >/dev/null; }  # checkin <id> <query>
said_nothing_secret() { ! docker compose logs --no-log-prefix checkin 2>&1 | grep -q -- "$KEY"; }

cleanup() {
  if [ "$KEEP" = 0 ]; then
    curl -s -X POST "$VM_URL/api/v1/admin/tsdb/delete_series" --data-urlencode 'match[]={device_id=~"ck-.*"}' >/dev/null
    curl -s -X POST "$VM_URL/internal/resetRollupResultCache" >/dev/null
  fi
}
trap cleanup EXIT

cd "$ROOT" || exit 2
printf 'Check-in URL end-to-end check (about 3 minutes)\n\n'

# ---- 1. set it up the way a user does: fleet up, then add a device by check-in
if fleet up >/tmp/validate_checkin_up.log 2>&1; then refresh_auth; pass "./fleet up"; else fail "./fleet up" "see /tmp/validate_checkin_up.log"; cat /tmp/validate_checkin_up.log; exit 1; fi
out=$(fleet add ck-slow --checkin --every 60 --host localhost --no-wait 2>&1); rc=$?
check "./fleet add --checkin starts the service and prints a command" test "$rc" = 0
KEY=$(sed -n 's/^CHECKIN_KEY=//p' .env)
PORT=$(sed -n 's/^CHECKIN_PORT=//p' .env); PORT=${PORT:-8081}
BASE="http://localhost:$PORT"
check "a key was created (24 hex characters)" test "${#KEY}" = 24
check "the command it printed is the one we are about to use" has "$out" "curl -fsS -m 10 \"$BASE/checkin/ck-slow?key=$KEY&every=60\""
check "the service answers on /health" test "$(code_of "$BASE/health")" = 200

# ---- 2. refusals
check "a wrong key is refused (401)" test "$(code_of "$BASE/checkin/ck-bad?key=wrong&every=60")" = 401
check "no key is refused (401)" test "$(code_of "$BASE/checkin/ck-bad")" = 401
check "a bad device id is refused (400)" test "$(code_of "$BASE/checkin/Bad%20Id?key=$KEY")" = 400
check "a bad reading is refused (400)" test "$(code_of "$BASE/checkin/ck-bad?key=$KEY&temperature_celsius=hot")" = 400
check "an interval that is too long is refused (400)" test "$(code_of "$BASE/checkin/ck-bad?key=$KEY&every=99999")" = 400
check "an unknown address is a 404" test "$(code_of "$BASE/nope")" = 404
sleep 7
check "nothing was stored for the refused ones" test "$(query 'count({device_id="ck-bad"})' | wc -l)" = 0

# ---- 3. check in with curl
slow_checked=$(now)
checkin ck-slow "every=60&temperature_celsius=21.5&battery_volts=3.9"
checkin ck-quick "temperature_celsius=19"
quick_checked=$(now)
started=$(now)
if wait_until 60 is_state ck-slow ONLINE; then pass "ck-slow appears ONLINE" "after $(( $(now) - started )) s"; else fail "ck-slow appears ONLINE" "$(state_of ck-slow)"; fi
if wait_until 60 is_state ck-quick ONLINE; then pass "ck-quick appears ONLINE"; else fail "ck-quick appears ONLINE" "$(state_of ck-quick)"; fi
check "both are reported via checkin" test "$(field_of ck-slow via)/$(field_of ck-quick via)" = checkin/checkin
labels=$(query 'device_uptime_seconds{device_id="ck-slow"}' | head -1 | python3 -c '
import json, sys
m = json.loads(sys.stdin.read().rsplit(" ", 1)[0]); print(",".join(sorted(m)), m.get("job"))')
check "stored labels are exactly device_id and job=checkin" test "$labels" = "__name__,device_id,job checkin"
check "the readings are stored (21.5 and 3.9)" test "$(scalar 'device_temperature_celsius{device_id="ck-slow"}')/$(scalar 'device_battery_volts{device_id="ck-slow"}')" = 21.5/3.9
check "the declared interval is stored (60)" test "$(scalar 'device_report_interval_seconds{device_id="ck-slow"}')" = 60
check "a device that declared nothing has no interval series" test "$(query 'device_report_interval_seconds{device_id="ck-quick"}' | wc -l)" = 0
check "the key is not in the service's log" said_nothing_secret

# ---- 4. the slow device is still fine 45 s after its only check-in
while [ $(($(now) - slow_checked)) -lt 45 ]; do sleep 1; done
check "a device that declared every=60 is still ONLINE after 45 s of silence" is_state ck-slow ONLINE

# ---- 5. the quiet one goes OFFLINE, alerts, and comes back
if wait_until 90 is_state ck-quick OFFLINE; then pass "a device that stops checking in goes OFFLINE" "$(( $(now) - quick_checked )) s after its only check-in (rule: 30 s)"; else fail "ck-quick goes OFFLINE" "$(state_of ck-quick)"; fi
if wait_until 150 alerting ck-quick; then pass "DeviceOffline fires for it" "$(( $(now) - quick_checked )) s after its only check-in"; else fail "DeviceOffline fires for ck-quick" "alert: $(alert_state ck-quick)"; fi
check "no alert for the slow one (still inside its own limit)" not_alerting ck-slow
checkin ck-quick "temperature_celsius=19"
check "a new check-in brings it back ONLINE" wait_until 40 is_state ck-quick ONLINE
check "...and its uptime started over (under 40 s)" test "$(field_of ck-quick uptime | cut -d. -f1)" -lt 40
check "the alert clears" wait_until 90 not_alerting ck-quick

# ---- 6. the off switch
fleet up --no-checkin >/dev/null 2>&1
check "./fleet up --no-checkin stops the service" test "$(code_of "$BASE/health")" = 000
check "...but keeps the key, so the commands already handed out work again" test "$(sed -n 's/^CHECKIN_KEY=//p' .env)" = "$KEY"
fleet add ck-again --checkin --every 60 --host localhost --no-wait >/dev/null 2>&1
check "turning it on again works with the same key" test "$(code_of "$BASE/checkin/ck-again?key=$KEY&every=60")" = 200

printf '\n%d/%d steps passed\n' "$OK" "$RUN"
[ "$FAILED" = 0 ] && [ "$OK" = "$RUN" ]
