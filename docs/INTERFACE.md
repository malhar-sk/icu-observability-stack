# Ask the stack questions: an MCP server and an HTTP API (read-only)

Two ways to ask the same four questions about your fleet, from a script or from an AI assistant:

```
 AI assistant (an MCP client) --pipes--> interface/mcp_server.py --+
                                                                   +--> the questions --> ./fleet logic --> VictoriaMetrics
 script, curl, a dashboard -----HTTP---> interface/http_api.py ----+     (interface/features/)
```

**It cannot change anything.** There is no tool or endpoint that adds, removes, restarts or configures anything, and the
tests and a live run check that. Demonstration-grade, like the rest of this repo; not for medical or safety use.

## Why it exists

Setting a device up takes a few commands and a look at whether it arrived. An assistant that can *see* the fleet (is the
server up? is MQTT on? did the new board show up? why is it offline?) and can *explain* the exact steps for each kind of
device is much more useful than one guessing. A person, or an assistant with a shell on the server, still runs the
commands; this only answers questions.

## Written from scratch, on purpose

The MCP server is about 260 lines of Python using only the standard library. It does **not** use the official MCP SDK.
The reason is supply chain: `pip install mcp` brings in 27 other packages (measured on mcp 2.3.0, in a clean
environment: a web server, an HTTP client, validation and cryptography libraries and their own dependencies), and a flaw or a
poisoned release in any of them would run on your Pi and your laptop. This has none: nothing to install, nothing to
update, nothing to be surprised by. The HTTP API and the questions are standard library too.

The cost is that **we own the protocol code**. To keep that honest:

- it implements only what a read-only tool server needs (`initialize`, `ping`, `tools/list`, `tools/call`, over stdio),
  and answers anything else with a standard "method not found";
- it is tested three ways (see [Tests](#tests)), including against the **official client** as an independent referee. The
  official package is needed only to run those few referee tests; the server never imports it;
- it hardens the pipes: a 1 MB limit on a message, strict JSON (no `NaN`, no bad UTF-8), a malformed message gets an error
  and the next one is still answered, and stdout is taken over at the file-descriptor level, so a stray `print` or child
  process cannot corrupt the stream.

I could not read the protocol specification itself from the build machine (the site was not reachable), so the wire
format was taken from the official SDK's own type definitions and then checked by running the official client against
this server. If a client you use disagrees, that is a bug to report.

## Use it

### The HTTP API

```bash
python3 interface/http_api.py                       # http://127.0.0.1:8765, this machine only
curl http://127.0.0.1:8765/v1                       # the questions, with their parameters
curl http://127.0.0.1:8765/v1/stack_status
curl "http://127.0.0.1:8765/v1/list_devices?state=offline"
curl "http://127.0.0.1:8765/v1/get_device?device_id=kitchen-pi"
curl "http://127.0.0.1:8765/v1/how_to_add_device?kind=checkin&device_id=backup-job"
```

Run it on the machine that hosts the stack. Needs Python 3.9+, nothing else. Answers are JSON; a problem is
`{"error": "..."}` with a status: 400 for a bad request, 404 for an unknown question, 503 when the stack is down (the
message says `./fleet up`), 401/403 for the access rules below, 405 for anything but GET.

### The MCP server (for an AI assistant)

```bash
python3 interface/mcp_server.py --print-config      # prints the settings to paste into your MCP client
```

It prints a `mcpServers` block with the full path to this Python and this file (for Claude Desktop's
`claude_desktop_config.json`, or any client that takes the same shape) and, for Claude Code, a one-line
`claude mcp add icu-fleet -- ...` command. The client starts the server itself, so there is nothing to run by hand and no
port to open. Needs Python 3.9+ (the code was checked for 3.9 syntax, but it has only been run on 3.13).

*Tested with the official MCP client from Anthropic's SDK, both its default mode and its older `initialize` mode, not
with the Claude Desktop or Claude Code apps themselves.*

**From another machine** (an assistant on your laptop, the stack on the Pi): the server reads the stack on `localhost`, so
it has to run on the Pi. Let the client start it over SSH, so no port is opened at all: in the client's settings use
`"command": "ssh"` with `"args": ["pi@PI-ADDRESS", "python3", "/home/pi/icu-observability-stack/interface/mcp_server.py"]`.
*Not tried.* The HTTP API can also be reached over the network, with a token (below).

## The four questions

| Name | Asks | Notes |
|---|---|---|
| `stack_status` | Is the server up? Which inputs are on (MQTT, check-in URL, watching)? How many devices are online? | Works when the stack is down, and then says what is down. Gives the address and ports devices use. |
| `list_devices` | The devices that reported in the last 24 hours, with state and readings. `state=online` or `offline` to filter. | Units are in the names (`cpu_percent`, `temperature_celsius`); a reading a device does not report is `null`. |
| `get_device` | One device in detail: state, **why**, what to try. Also how to check that a device you just set up arrived. | A device that has not reported is `found: false` with the known IDs (not an error). New ones can take about 15 s to appear. |
| `how_to_add_device` | The exact steps for `agent`, `mqtt`, `checkin` or `watch`; with no `kind`, which way fits which device. | Pass `device_id` and the commands come filled in. Shows `<CHECKIN_KEY>`, never the key. |

"Online" is **not decided here**. It is the same rule `./fleet list`, the dashboard and the alert use
(`docs/CONTRACT.md`): offline after 3 times the device's own reporting interval, never under 30 s. The interface calls
`./fleet`'s own code, and a live check compares it with `./fleet list` for every device.

### How an assistant sets up a device

1. `stack_status`: is the server up, and is the input this device needs turned on?
2. `how_to_add_device` with the `kind` and a `device_id`: exact commands, marked `server` (run on the machine that hosts the
   stack, by a person or an assistant with a shell there) or `device` / `board`.
3. After the commands run, `get_device`: `ONLINE` means it worked. `found: false` after about 15 s means it did not;
   `explanation` and `next_steps` say what to look at.

## Safety

| Rule | Where it is enforced |
|---|---|
| Nothing can change anything | There are no such features. The API accepts only GET (everything else is 405). Every MCP tool is marked read-only. A live check compares `.env`, the watch list and every container before and after. |
| No password, key or token is ever returned | Features do not read them. As a net underneath, **every answer is searched for the secrets in `.env` and the API token before it leaves**; a match refuses the answer (it becomes a generic error, nothing is leaked in the message or the log). Values under 8 characters are skipped, because they would match ordinary words, so a very short password you chose yourself is not covered by this net (it is still never read by a feature). |
| The API is not on the network by default | It listens on `127.0.0.1`. Any other address needs `FLEET_API_TOKEN` (16+ characters; never taken from the command line), and then every request must send `Authorization: Bearer <token>`. A token in the address is not accepted. |
| A web page cannot read it through your browser | On localhost the `Host` header must be `localhost`, `127.0.0.1` or `[::1]` (guards against DNS rebinding). No CORS headers are sent. |
| Logs hold no secrets | The API logs method, path and status only, never the query string, and scrubs the token from any other message. |
| Bad input cannot hurt it | Argument types are checked strictly (`true` is not `1`), text is capped at 256 characters, unknown parameters are errors (a typo like `?stat=offline` does not quietly return everything), a request line is capped, and a bug gives "internal error" with the details in the log only. |
| No TLS | The API is plain HTTP. To use it across a network you do not trust, put something that adds TLS in front of it. |

Anyone with a shell on the same machine can use the localhost API: it is as private as that machine.

## Add or remove a question

Everything the two front-ends offer comes from one folder, `interface/features/`. Each question is a function marked
`@feature`. **Add one** by adding a file, **remove one** by deleting its file, **switch one off** without deleting it with
`FLEET_INTERFACE_DISABLE=get_device,how_to_add_device` (in both front-ends; a name that is not a feature stops the server
with an error, because a typo in a safety switch must not pass silently). Neither front-end needs editing.

A complete new question, as a new file `interface/features/offline_count.py`:

```python
from interface import fleetlink
from interface.registry import feature


@feature
def offline_count() -> dict:
    """How many devices are offline right now.

    Use it for a quick yes or no before looking at list_devices."""
    found = fleetlink.devices()
    return {"offline": sum(1 for d in found.values() if d["state"] != "ONLINE")}
```

Restart the server and `offline_count` is a tool and `GET /v1/offline_count`. The rules, checked when the file loads
(a mistake stops the server at start, with the reason):

- the function has a **docstring** (the first line is the short description; the rest is what the assistant reads);
- parameters have type hints of `str`, `int`, `float` or `bool`: they become the tool's schema and the query parameters;
- it returns a **dict** that is valid JSON;
- it gets data only through `interface/fleetlink.py` (the one module that talks to `./fleet` and the stack), and only
  reads. A feature that changes things does not belong here: that would be a separate, deliberate decision.

Add a test next to the others in `tests/test_interface.py` (the `WithStack` class gives you a fake stack).

## What is where

| File | Job |
|---|---|
| `interface/features/*.py` | The questions. One small function each. |
| `interface/registry.py` | The list of features, the argument checks, the secrets check. Both front-ends call `registry.call`. |
| `interface/fleetlink.py` | The only code that talks to `./fleet` and the stack. |
| `interface/mcp_server.py` | MCP front-end, in three parts: tools, protocol, transport. |
| `interface/http_api.py` | HTTP front-end and its access rules. |

## Tests

- `tests/test_interface.py` (66): the registry, every feature against a fake stack, the secrets check, the strict
  arguments, the HTTP API and its access rules and its log.
- `tests/test_interface_mcp.py` (44): the protocol rules called directly; the real program over real pipes with only the
  standard library (bad input, oversized and cut-off messages, non-ASCII text, stdout kept clean, runs with `python -S`
  so no installed package can be what makes it work); and the **official client as referee** (skipped when `pip install
  mcp` has not been done).
- `scripts/validate_interface.sh` (51 checks, about 3 minutes): the real stack in Docker, devices added with `curl`, the
  real API process and the real MCP server, checked against `./fleet list`, for leaked secrets, for unchanged state, and
  for the network rules. Results: [VALIDATION.md](VALIDATION.md), run L.

## Not covered

- Claude Desktop, Claude Code or any other real assistant app has not been connected to it. The official SDK's client has.
- Running the MCP server over SSH from another machine (above) was not tried; nor was the API with a token over a real
  network, TLS, or IPv6 (this machine could not bind `::1`).
- The protocol specification itself was not read (see above). Protocol versions 2024-11-05 to 2025-11-25 are accepted; the
  newer stateless handshake is answered with "method not found" so clients fall back to the older one.
- Python 3.9 to 3.12 and macOS: only 3.13 on Linux was run.
- It answers questions about the last 24 hours only, like `./fleet list`.
- Only the four questions exist. Alerts, the notification channel's details and the watch list's contents are not exposed
  (`stack_status` says only whether alerts go to a channel, and how many things are watched).
