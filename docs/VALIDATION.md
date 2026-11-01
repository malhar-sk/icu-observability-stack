# Validation report

One validation session on a single machine. **No human walked through the quickstart and nothing
was run on a Raspberry Pi or on arm64**; see the items marked PARTIAL and the "Not validated" list.
Screenshots referenced below are in [docs/img](img/).

## Environment

| Item | Value |
|---|---|
| Date / time (UTC) | 2026-11-01, 06:21 to 06:33 |
| Validated by | An AI coding agent (Claude Code session), not a person. Browser checks used headless Chromium driven by Playwright. |
| Git commit tested | Two runs. **Run A** (manual walkthrough): the working tree at `4f2c6ae` plus README-only edits. **Run B** (fresh clone, `git clone -b claude/happy-franklin-k77hxk`): `29ba7fc`. Only `README.md` and `docs/CONTRACT.md` differ between them; no agent, compose or Grafana file does. This report and `docs/img/` were added afterwards. |
| Host | x86_64 Linux VM, 4 vCPU, 16 GB RAM. Not a Raspberry Pi. |
| OS | Ubuntu 24.04.5 LTS |
| Docker / Compose | 29.9.2 / v5.6.1 |
| Python | 3.13.18 (agent packages installed: opentelemetry-sdk and exporter 1.45.2) |
| Clean start | Yes. `docker compose down -v` before run A, and again before the `validate.sh` part of run B, so no earlier data. |
| Remote device | None. All devices ran on the same machine, distinguished by `--device-id`. |

## Commands run

Run A (steps 1 to 6 in detail) was in the working tree; run B (steps 1, 2, 5 first-row timing, 7, 8)
was in the fresh clone. Both started from empty volumes. Two "terminals" were two background
processes; Ctrl+C was SIGINT.

| # | Command | Outcome |
|---|---|---|
| 1 | `docker compose up -d` | 3 containers started: victoriametrics, otel-collector, grafana (no Vector). Datasource, dashboard and alert rule were provisioned with no manual step. |
| 2 | `python3 -m venv .venv && . .venv/bin/activate && pip install -r agent/requirements.txt` | Succeeded; OTel SDK 1.45.2 imports. |
| 3 | `python agent/device_agent.py --device-id demo-pi-1` | Started, logged "no readable temperature sensor" (expected here), pushed every 5 s. |
| 4 | `python agent/device_agent.py --device-id demo-pi-2` | Same. |
| 5 | Open <http://localhost:3000> (`admin` / `admin`) | Home page is the Device Fleet dashboard. Both rows visible 15.2 s after the agents started (measured in the browser). |
| 6 | SIGINT to `demo-pi-2` (run A) | OFFLINE on the dashboard 36.2 s later; `DeviceOffline` Firing for `demo-pi-2` only 69 s later. Restarting it: ONLINE and Normal again after 12.1 s. |
| 7 | `./scripts/validate.sh` (from empty volumes) | Exit code 0, 18 of 18 steps passed. |
| 8 | `docker compose down`, then `docker compose up -d` | Containers removed, volumes kept; `demo-pi-1` and `demo-pi-2` still queryable in VictoriaMetrics afterwards; dashboard still present. |

## Definition of Done

| # | Item | Result | Evidence |
|---|---|---|---|
| 1 | Stack starts with the documented command | PASS | `docker compose ps`: all three services running on the pinned images. VictoriaMetrics `/health` OK, Grafana `/api/health` database ok (both also `PASS` lines in `validate.sh`). |
| 2 | Agent sends telemetry | PASS | VictoriaMetrics held `device_cpu_/memory_/disk_utilization_percent` and `device_uptime_seconds` for both devices. Agent unit tests: 33 passed. Agent also ran against the declared minimum OTel version (1.25.0). |
| 3 | Two distinct devices on one dashboard | PASS | Two rows, `demo-pi-1` and `demo-pi-2` ([01](img/01-two-devices-online-fresh-clone.png)). Series have `instance` equal to `device_id` and `job="device-agent"`. |
| 4 | Health metrics visible | PARTIAL | CPU, memory, disk, uptime visible for each device. Temperature: no sensor exists on this VM. Verified only with the real agent code reading a **fake** sysfs directory: 52.5 °C shown for that device and "N/A" for the sensorless ones ([04](img/04-temperature-mixed-fleet.png)). Not verified on real hardware. |
| 5 | Last-seen updates | PASS | The same online device showed last seen 06:22:44, then 06:24:49, then 06:26:19 in three screenshots taken minutes apart. |
| 6 | Offline is distinguishable | PASS | `demo-pi-2` turned red OFFLINE with last seen equal to the stop time while `demo-pi-1` stayed green ([02](img/02-demo-pi-2-offline.png)). Measured 36.2 s (manual) and 36 s (`validate.sh`). |
| 7 | Alert works | PASS | `DeviceOffline` (Provisioned) Firing for the silent device only; `demo-pi-1` Normal ([03](img/03-alert-firing-for-silent-device.png)). Measured 69 s (manual), 71 s (`validate.sh`); Pending seen on the way. Recovery to Normal 12.1 s after restart. No notification is sent: no contact point is configured. |
| 8 | Quickstart tested | PARTIAL | The README commands were executed verbatim in a fresh clone and reached the firing alert, but by an agent, not a person who had never seen the project. The clone used `-b claude/happy-franklin-k77hxk` because the branch is not merged to `main`. |
| 9 | Changes committed | PASS | Committed and pushed to `claude/happy-franklin-k77hxk`; `git status` is clean after the commit that adds this file (`git log` lists it). |

## Automated check

| Item | Value |
|---|---|
| Command | `./scripts/validate.sh` (defaults; run from the fresh clone, empty volumes) |
| Exit code | 0 |
| Steps passed | 18 of 18 |
| Seconds from stopping the agent to OFFLINE | 36 |
| Seconds from stopping the agent to alert Firing | 71 |

It checks the data path, offline detection and the alert through the APIs. It does not look at the
rendered dashboard; that was checked separately with screenshots.

## Remote device

| Check | Result | Evidence |
|---|---|---|
| Agent on a remote device reached `<server-ip>:4317` | NOT RUN | No second machine was available. |
| It appears as a separate row with its own device ID | NOT RUN | As above. |

## Logs

| Source | Finding |
|---|---|
| otel-collector | No errors. |
| VictoriaMetrics | No errors. One INFO line that cgroup pressure metrics are unavailable (container quirk). |
| Grafana | Error-level lines are all its background plugin installer failing to resolve `grafana.com`; this sandbox blocks that host. Harmless for the demo. INFO-level "database is locked, retrying" on first boot. No provisioning errors. |
| Agents | Clean start and clean shutdown. |

## Issues found

| # | Description | Severity | Status |
|---|---|---|---|
| 1 | Original compose mounted `vm_data` at `/storage` but VictoriaMetrics wrote elsewhere, so metrics were lost when containers were recreated. | High | Fixed (`-storageDataPath=/storage`); verified by `down` then `up`. |
| 2 | VictoriaMetrics hides the newest 30 s of data by default, delaying new devices and offline detection. | Medium | Fixed (`-search.latencyOffset=5s`). |
| 3 | The Vector service exits at startup (wrong config path, and its config wires logs into a metrics sink). | Low | Not fixed. Parked behind compose profile `legacy-vector`. |
| 4 | No Grafana dashboard or alert rules existed in the repository; the README described ones that only lived in someone's Grafana volume. | Medium | Built new, provisioned from files. The original "ICU Floor 1" dashboard is still not in the repo. |
| 5 | The temperature column is absent when no device reports a temperature (as on this VM). | Low | Documented, not changed. A Raspberry Pi has a sensor. |
| 6 | Graphs show "No data" for the first ~20 to 30 s after a device starts. | Low | Documented. |
| 7 | README step 4 omitted that the alert group is collapsed and that per-device states are on the rule page. | Low | Fixed. |
| 8 | Alert instances for test device IDs persist for a day, cluttering the rule page after test runs. | Low | Known; `docker compose down -v` clears it. |

## Not validated

- A real Raspberry Pi, any arm64 host, or a real temperature sensor.
- A second physical machine pushing over a network, or any firewall behaviour.
- A person following the quickstart cold.
- Notification delivery (none is configured).
- Behaviour with more than a handful of devices, long retention, or restarts of the agent across reboots.
