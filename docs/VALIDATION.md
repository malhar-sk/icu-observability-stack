# Validation report

Template. Fill in after a run. Replace `NOT YET RUN` only with what you actually observed, and put
the evidence (command output, a log excerpt, a screenshot filename or a timestamp) next to it.
Use `PASS`, `FAIL` or `PARTIAL` as the result, and say why for anything other than PASS.

## Environment

| Item | Value |
|---|---|
| Date / time (UTC) | NOT YET RUN |
| Validated by | NOT YET RUN |
| Git commit (`git rev-parse --short HEAD`) | NOT YET RUN |
| Host (model, CPU architecture: `uname -m`) | NOT YET RUN |
| OS (`cat /etc/os-release`) | NOT YET RUN |
| Docker (`docker --version`) | NOT YET RUN |
| Compose (`docker compose version`) | NOT YET RUN |
| Python (`python3 --version`) | NOT YET RUN |
| Clean start (`docker compose down -v` before the run)? | NOT YET RUN |
| Remote device used (model, OS, network), or "none, same machine" | NOT YET RUN |

## Commands run

List the exact commands in the order you ran them, with the outcome of each.

| # | Command | Outcome |
|---|---|---|
| 1 | `docker compose up -d` | NOT YET RUN |
| 2 | `python3 -m venv .venv && . .venv/bin/activate && pip install -r agent/requirements.txt` | NOT YET RUN |
| 3 | `python agent/device_agent.py --device-id demo-pi-1` (terminal 1) | NOT YET RUN |
| 4 | `python agent/device_agent.py --device-id demo-pi-2` (terminal 2) | NOT YET RUN |
| 5 | Open <http://localhost:3000> (`admin` / `admin`), check the Device Fleet dashboard | NOT YET RUN |
| 6 | Ctrl+C the `demo-pi-2` agent, watch the table and the DeviceOffline alert | NOT YET RUN |
| 7 | `scripts/validate.sh` (full output and exit code) | NOT YET RUN |
| 8 | `docker compose down` | NOT YET RUN |

## Definition of Done

| # | Item | How to check | Result | Evidence |
|---|---|---|---|---|
| 1 | Stack starts with the documented command | `docker compose up -d`, then `docker compose ps` shows victoriametrics, otel-collector and grafana Up; `curl localhost:8428/health` prints `OK`; `curl localhost:3000/api/health` reports `"database": "ok"` | NOT YET RUN | |
| 2 | Agent sends telemetry | Agent starts without error; `curl -s 'localhost:8428/api/v1/query?query=device_uptime_seconds'` returns a series for the device | NOT YET RUN | |
| 3 | Two distinct devices on one dashboard | With `demo-pi-1` and `demo-pi-2` running, the Device Fleet table shows two rows with different device IDs; the VictoriaMetrics series have `instance` equal to `device_id` and `job="device-agent"` | NOT YET RUN | |
| 4 | Health metrics visible | The table and graphs show CPU %, memory %, disk %, uptime for each device; temperature shows a value, or "N/A" on a machine without `/sys/class/thermal` | NOT YET RUN | |
| 5 | Last-seen updates | The "last seen" value advances while the agent runs (compare two screenshots or two queries a few seconds apart) | NOT YET RUN | |
| 6 | Offline is distinguishable | After stopping `demo-pi-2`, its state becomes OFFLINE within roughly 30 to 40 s while `demo-pi-1` stays ONLINE; note the measured delay | NOT YET RUN | |
| 7 | Alert works | In Alerting, Alert rules, `DeviceOffline` goes Pending then Firing for `device_id=demo-pi-2` only (roughly a minute after the stop); note the measured delay; starting the agent again returns it to Normal | NOT YET RUN | |
| 8 | Quickstart tested | A person (or a fresh machine) followed the README Quickstart from the top, without other help, and reached the offline alert; note anything that was unclear | NOT YET RUN | |
| 9 | Changes committed | `git status` is clean for the deliverables and `git log` shows the commit(s) | NOT YET RUN | |

## Automated check

`scripts/validate.sh` runs the data path, the offline detection and the alert end to end (items 1 to 3,
5 to 7 in machine-checkable form; it does not look at the rendered dashboard). Paste its summary here.

| Item | Value |
|---|---|
| Command (include any environment overrides) | NOT YET RUN |
| Exit code | NOT YET RUN |
| Steps passed | NOT YET RUN |
| Measured: seconds from stopping the agent to OFFLINE | NOT YET RUN |
| Measured: seconds from stopping the agent to alert Firing | NOT YET RUN |

## Remote device (optional)

Only if a real second machine was used (see the README, "Monitoring a real remote Raspberry Pi").

| Check | Result | Evidence |
|---|---|---|
| Agent on the remote device reached `<server-ip>:4317` | NOT YET RUN | |
| It appears as a separate row with its own device ID | NOT YET RUN | |

## Issues found

| # | Description | Severity | Status |
|---|---|---|---|
| | NOT YET RUN | | |
