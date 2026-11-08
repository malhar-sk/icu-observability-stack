#!/usr/bin/env bash
# End-to-end check of the read-only interface (the HTTP API and the MCP server) against the real stack.
#
#   scripts/validate_interface.sh [--keep] [--help]
#
# Starts the stack (./fleet up), adds two check-in devices with curl, runs the real HTTP API and the real MCP server
# (over real pipes, standard library only), and proves:
#   - both answer the same questions, and agree with `./fleet list` about every device
#   - a device that goes quiet shows as OFFLINE with the reason and what to try, in both
#   - setup guidance is filled in with this machine's address and the check-in port
#   - no password, key or token appears in anything either one returns
#   - nothing changed: .env, the watch list and the containers are identical before and after, and POST is refused
#   - the network rules hold: no listening beyond this machine without a token, 401 without it, 403 for a foreign Host
#
# Needs: Docker with Compose v2, python3, curl. Takes about 3 minutes. Exits 0 only if every step passes.
# Never runs `docker compose down -v`. The test devices (ifc-*) are removed at the end unless --keep is given.
#
# Environment overrides (defaults in brackets): VM_URL [http://localhost:8428]  API_PORT [18765]

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VM_URL="${VM_URL:-http://localhost:8428}"
API_PORT="${API_PORT:-18765}"
API="http://127.0.0.1:$API_PORT"
NET_PORT=$((API_PORT + 1))
KEEP=0
case "${1-}" in
  --keep) KEEP=1 ;;
  -h|--help) sed -n '2,/^set -uo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
  "") ;;
  *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
esac

if [ -t 1 ]; then C_PASS=$'\033[32m'; C_FAIL=$'\033[31m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'; else C_PASS=""; C_FAIL=""; C_DIM=""; C_OFF=""; fi
RUN=0; OK=0; FAILED=0
WORK=$(mktemp -d)
RAW="$WORK/everything-returned.txt"   # every answer either front-end gave, to search for secrets at the end
: >"$RAW"
PIDS=()

pass() { RUN=$((RUN + 1)); OK=$((OK + 1)); printf '%s PASS%s  %s %s%s%s\n' "$C_PASS" "$C_OFF" "$1" "$C_DIM" "${2-}" "$C_OFF"; }
fail() { RUN=$((RUN + 1)); FAILED=1; printf '%s FAIL%s  %s %s\n' "$C_FAIL" "$C_OFF" "$1" "${2-}"; }
check() { local name=$1; shift; if "$@"; then pass "$name"; else fail "$name"; fi; }
fleet() { "$ROOT/fleet" "$@"; }
now() { date +%s; }
has() { grep -qi -- "$2" <<<"$1"; }
lacks() { ! grep -qF -- "$1" <<<"$2"; }       # lacks <secret> <text>
lacks_in_file() { ! grep -qF -- "$1" "$2"; }  # lacks_in_file <secret> <file>
wait_until() {
  local limit=$1 start; shift; start=$(now)
  while ! "$@" >/dev/null 2>&1; do [ $(($(now) - start)) -ge "$limit" ] && return 1; sleep 1; done
  return 0
}
# json <text> <python expression using d>   -> prints the value (or "ERR" when the text is not JSON)
json() { python3 -c 'import json,sys
try:
    d = json.loads(sys.argv[1]); print(eval(sys.argv[2]))
except Exception:
    print("ERR")' "$1" "$2"; }
api() { local out; out=$(curl -s -m 15 "$API$1" "${@:2}"); printf '%s\n' "$out" >>"$RAW"; printf '%s' "$out"; }  # api </path> [curl options]
code_of() { curl -s -o /dev/null -w '%{http_code}' -m 10 "$@"; }
state_in_cli() { fleet list --json 2>/dev/null | python3 -c '
import json, sys
for d in json.load(sys.stdin):
    if d["device_id"] == sys.argv[1]:
        print(d["state"]); break
else:
    print("-")' "$1"; }
is_cli_state() { [ "$(state_in_cli "$1")" = "$2" ]; }
api_up() { [ "$(code_of "$API/v1")" = 200 ]; }
checkin() { curl -fsS -m 10 "$BASE/checkin/$1?key=$KEY&$2" >/dev/null; }
snapshot() {  # what the interface must never change
  { sha256sum "$ROOT/.env" "$ROOT/watch/targets.json" 2>/dev/null
    docker compose --profile '*' ps -a --format '{{.Service}} {{.ID}} {{.State}}' 2>/dev/null | sort; }
}

cleanup() {
  for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null; done
  if [ "$KEEP" = 0 ]; then
    curl -s -X POST "$VM_URL/api/v1/admin/tsdb/delete_series" --data-urlencode 'match[]={device_id=~"ifc-.*"}' >/dev/null
    curl -s -X POST "$VM_URL/internal/resetRollupResultCache" >/dev/null
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

cd "$ROOT" || exit 2
printf 'Read-only interface end-to-end check (about 3 minutes)\n\n'

# ---- 1. a real stack with two devices
if fleet up >"$WORK/up.log" 2>&1; then pass "./fleet up"; else fail "./fleet up" "see below"; cat "$WORK/up.log"; exit 1; fi
fleet add ifc-slow --checkin --every 60 --host localhost --no-wait >/dev/null 2>&1
KEY=$(sed -n 's/^CHECKIN_KEY=//p' .env); PORT=$(sed -n 's/^CHECKIN_PORT=//p' .env); PORT=${PORT:-8081}
BASE="http://localhost:$PORT"
checkin ifc-slow "every=60&temperature_celsius=21.5"
checkin ifc-quiet "temperature_celsius=19"          # declares no interval: held to the 30 s floor
quiet_checked=$(now)
check "both devices appear in ./fleet list" wait_until 60 bash -c "[ \"\$($ROOT/fleet list --json | python3 -c 'import json,sys; print(sum(d[\"device_id\"].startswith(\"ifc-\") and d[\"state\"]==\"ONLINE\" for d in json.load(sys.stdin)))')\" = 2 ]"
check "the check-in key is long enough to be worth hiding" test "${#KEY}" -ge 16
BEFORE=$(snapshot)

# ---- 2. the HTTP API, started the way the docs say
python3 interface/http_api.py --port "$API_PORT" 2>"$WORK/api.log" &
PIDS+=($!)
check "the API starts and answers on 127.0.0.1" wait_until 15 api_up
index=$(api /v1)
check "the index lists the four questions" test "$(json "$index" 'sorted(f["name"] for f in d["features"])')" = "['get_device', 'how_to_add_device', 'list_devices', 'stack_status']"
status=$(api /v1/stack_status)
check "stack_status: running, check-in on, devices counted" test "$(json "$status" '(d["running"], d["inputs"]["checkin_url"]["on"], d["devices"]["online"] >= 2)')" = "(True, True, True)"
check "stack_status says where devices should send data" has "$status" ':4317'
slow=$(api '/v1/get_device?device_id=ifc-slow')
check "get_device: ifc-slow is ONLINE, via check-in, with its reading" test "$(json "$slow" '(d["found"], d["state"], d["reports_via"], d["temperature_celsius"], d["offline_after_seconds"])')" = "(True, 'ONLINE', 'checkin', 21.5, 180.0)"
check "...and its limit is 3 x the 60 s it declared" has "$slow" "3 x the 1 min"
check "get_device on an unknown id is an answer, not an error" test "$(json "$(api '/v1/get_device?device_id=ifc-nope')" '(d["found"], "ifc-slow" in d["known_devices"])')" = "(False, True)"
guide=$(api "/v1/how_to_add_device?kind=checkin&device_id=ifc-new")
check "how_to_add_device fills in this server's check-in port and the new id" has "$guide" ":$PORT/checkin/ifc-new?key=<CHECKIN_KEY>"
check "...and shows a placeholder, not the real key" lacks "$KEY" "$guide"
check "a bad request is a 400 with a reason" test "$(code_of "$API/v1/list_devices?state=sleeping")" = 400
check "a typo'd parameter is a 400, not silently ignored" test "$(code_of "$API/v1/list_devices?stat=offline")" = 400
check "an unknown feature is a 404" test "$(code_of "$API/v1/delete_everything")" = 404

# ---- 3. a device that goes quiet
if wait_until 90 is_cli_state ifc-quiet OFFLINE; then pass "ifc-quiet goes OFFLINE in ./fleet list" "$(( $(now) - quiet_checked )) s after its only check-in"; else fail "ifc-quiet goes OFFLINE" "$(state_in_cli ifc-quiet)"; fi
quiet=$(api '/v1/get_device?device_id=ifc-quiet')
check "the API agrees: OFFLINE, with the reason" test "$(json "$quiet" '(d["state"], d["offline_after_seconds"])')" = "('OFFLINE', 30.0)"
check "...it says why (no interval declared) and what to try" has "$quiet" "has not said how often it reports"
check "...and that removing it is for a person, not this tool" has "$quiet" "cannot do that"
offline=$(api '/v1/list_devices?state=offline')
check "list_devices?state=offline includes it, and not the slow one" test "$(json "$offline" '("ifc-quiet" in [x["device_id"] for x in d["devices"]], "ifc-slow" in [x["device_id"] for x in d["devices"]])')" = "(True, False)"
check "the slow device is still ONLINE after the quiet one went OFFLINE (its own limit)" test "$(json "$(api '/v1/get_device?device_id=ifc-slow')" 'd["state"]')" = ONLINE
all=$(api /v1/list_devices)
cli_view=$(fleet list --json | python3 -c 'import json,sys; print(sorted((d["device_id"], d["state"]) for d in json.load(sys.stdin)))')
api_view=$(json "$all" 'sorted((x["device_id"], x["state"]) for x in d["devices"])')
check "the API and ./fleet list agree about every device and its state" test "$api_view" = "$cli_view"
checkin ifc-quiet "temperature_celsius=19"
check "a new check-in brings it back, visible in the API" wait_until 40 bash -c "[ \"\$(curl -s $API/v1/get_device?device_id=ifc-quiet | python3 -c 'import json,sys; print(json.load(sys.stdin)[\"state\"])')\" = ONLINE ]"

# ---- 4. the MCP server, over real pipes, with nothing installed
driver=$(python3 - "$ROOT" "$RAW" <<'PY'
import json, os, subprocess, sys
root, raw = sys.argv[1], sys.argv[2]
proc = subprocess.Popen([sys.executable, "-S", os.path.join(root, "interface", "mcp_server.py")], cwd=root,
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
log = open(raw, "a")
def send(message):
    proc.stdin.write((json.dumps(message) + "\n").encode()); proc.stdin.flush()
def ask(n, method, **params):
    send({"jsonrpc": "2.0", "id": n, "method": method, "params": params})
    line = proc.stdout.readline().decode()
    log.write(line)
    return json.loads(line)
out = {}
init = ask(1, "initialize", protocolVersion="2025-11-25", capabilities={}, clientInfo={"name": "validate", "version": "0"})
out["version"] = init["result"]["protocolVersion"]
send({"jsonrpc": "2.0", "method": "notifications/initialized"})
tools = ask(2, "tools/list")["result"]["tools"]
out["tools"] = sorted(t["name"] for t in tools)
out["all_read_only"] = all(t["annotations"]["readOnlyHint"] and not t["annotations"]["destructiveHint"] for t in tools)
def call(n, name, **arguments):
    return ask(n, "tools/call", name=name, arguments=arguments)["result"]
stack = call(3, "stack_status")
out["stack"] = [stack["isError"], stack["structuredContent"]["running"], stack["structuredContent"]["devices"]["online"] >= 2]
listed = call(4, "list_devices", state="online")
out["online"] = sorted(d["device_id"] for d in listed["structuredContent"]["devices"] if d["device_id"].startswith("ifc-"))
slow = call(5, "get_device", device_id="ifc-slow")
out["slow"] = [slow["structuredContent"]["state"], slow["structuredContent"]["reports_via"], json.loads(slow["content"][0]["text"]) == slow["structuredContent"]]
bad = call(6, "get_device")
out["bad_call"] = [bad["isError"], "needs: device_id" in bad["content"][0]["text"]]
guide = call(7, "how_to_add_device", kind="watch", device_id="ifc-router")
out["guide"] = any(s.get("command") == "./fleet watch add ifc-router ping <address>" for s in guide["structuredContent"]["steps"])
unknown = ask(8, "tools/call", name="remove_device", arguments={"device_id": "ifc-slow"})
out["no_remove_tool"] = unknown["error"]["code"]
discover = ask(9, "server/discover")
out["discover"] = discover["error"]["code"]
proc.stdin.close()
out["exit_code"] = proc.wait(timeout=20)
print(json.dumps(out))
PY
)
check "MCP: handshake agrees on the newest version" test "$(json "$driver" 'd["version"]')" = 2025-11-25
check "MCP: the same four tools, every one marked read-only" test "$(json "$driver" '(d["tools"], d["all_read_only"])')" = "(['get_device', 'how_to_add_device', 'list_devices', 'stack_status'], True)"
check "MCP: stack_status answers from the real stack" test "$(json "$driver" 'd["stack"]')" = "[False, True, True]"
check "MCP: list_devices sees both test devices" test "$(json "$driver" 'd["online"]')" = "['ifc-quiet', 'ifc-slow']"
check "MCP: get_device matches the API (ONLINE via check-in), text equals structured data" test "$(json "$driver" 'd["slow"]')" = "['ONLINE', 'checkin', True]"
check "MCP: a call with a missing argument is an error result that says what is missing" test "$(json "$driver" 'd["bad_call"]')" = "[True, True]"
check "MCP: setup guidance carries the device id" test "$(json "$driver" 'd["guide"]')" = True
check "MCP: there is no tool that removes anything (unknown tool, -32602)" test "$(json "$driver" 'd["no_remove_tool"]')" = -32602
check "MCP: server/discover is 'method not found' (-32601), so newer clients fall back" test "$(json "$driver" 'd["discover"]')" = -32601
check "MCP: the server exits cleanly when the client closes the pipe" test "$(json "$driver" 'd["exit_code"]')" = 0
if python3 -c 'import mcp' 2>/dev/null; then
  referee=$(python3 - "$ROOT" <<'PY'
import asyncio, os, sys
from mcp import Client, StdioServerParameters
async def main():
    params = StdioServerParameters(command=sys.executable, args=["-S", os.path.join(sys.argv[1], "interface", "mcp_server.py")],
                                   env={"PATH": os.environ.get("PATH", "/usr/bin")})
    async with Client(params) as c:   # default mode: probes server/discover, then falls back to initialize
        r = await c.call_tool("get_device", {"device_id": "ifc-slow"})
        s = await c.call_tool("stack_status", {})
        print(c.protocol_version, r.structured_content["state"], s.structured_content["running"], r.is_error or s.is_error)
asyncio.run(main())
PY
)
  check "the official MCP client (referee) connects, calls two tools and gets the same answers" test "$referee" = "2025-11-25 ONLINE True False"
else
  printf '  SKIP  the official MCP client (pip install mcp) is not installed here; it is only a referee\n'
fi

# ---- 5. nothing leaked, nothing changed
leaked=0
for secret in "$KEY" "$(sed -n 's/^GRAFANA_ADMIN_PASSWORD=//p' .env | tr -d "'")" "$(sed -n 's/^MQTT_PASSWORD=//p' .env | tr -d "'")"; do
  [ -n "$secret" ] && grep -qF -- "$secret" "$RAW" && leaked=1
done
check "no password or key appears in anything the API or MCP returned ($(wc -c <"$RAW") bytes searched)" test "$leaked" = 0
check "the API's own log has no key either" lacks_in_file "$KEY" "$WORK/api.log"
for method in POST PUT DELETE PATCH; do
  check "$method is refused (405)" test "$(code_of -X "$method" "$API/v1/stack_status")" = 405
done
AFTER=$(snapshot)
check ".env, the watch list and every container are exactly as before" test "$BEFORE" = "$AFTER"
check "the stack still has both devices (nothing was deleted)" test "$(fleet list --json | python3 -c 'import json,sys; print(sum(d["device_id"].startswith("ifc-") for d in json.load(sys.stdin)))')" = 2

# ---- 6. the network rules
check "a foreign Host header is refused (403): the DNS-rebinding guard" test "$(code_of -H 'Host: evil.example' "$API/v1")" = 403
refusal=$(python3 interface/http_api.py --host 0.0.0.0 --port "$NET_PORT" 2>&1 >/dev/null </dev/null); rc=$?
check "listening beyond this machine without a token is refused" test "$rc" != 0
check "...and the refusal says what to do" has "$refusal" "FLEET_API_TOKEN"
short=$(FLEET_API_TOKEN=short python3 interface/http_api.py --port "$NET_PORT" 2>&1 >/dev/null </dev/null); rc=$?
check "a short token is refused, with the length it needs" test "$rc" != 0 -a "$(has "$short" "at least 16" && echo yes)" = yes
TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(16))')
FLEET_API_TOKEN=$TOKEN python3 interface/http_api.py --host 0.0.0.0 --port "$NET_PORT" 2>"$WORK/net.log" &
PIDS+=($!)
net_up() { [ "$(code_of "http://127.0.0.1:$NET_PORT/v1")" = 401 ]; }
check "with a token it listens, and an anonymous request gets 401" wait_until 15 net_up
check "a wrong token gets 401" test "$(code_of -H 'Authorization: Bearer nope' "http://127.0.0.1:$NET_PORT/v1/stack_status")" = 401
check "the token in the address does not work" test "$(code_of "http://127.0.0.1:$NET_PORT/v1/stack_status?token=$TOKEN")" = 401
check "the right token gets 200" test "$(code_of -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$NET_PORT/v1/stack_status")" = 200
check "the token is not in the server's log" lacks_in_file "$TOKEN" "$WORK/net.log"

printf '\n%d/%d steps passed\n' "$OK" "$RUN"
[ "$FAILED" = 0 ] && [ "$OK" = "$RUN" ]
