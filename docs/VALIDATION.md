# Validation report

Six runs. Run D tested the `./fleet` helper, run E its `notify` command and run F the start-at-boot service installer (see their own sections below). Runs A and B were on an x86_64 VM and were driven by an AI agent. Run C was on a
**Raspberry Pi 5** and was run by the Pi's owner at the keyboard over SSH, with an AI supplying the
commands step by step. **Nobody has followed the README cold, and no separate device has pushed to
the server over the network**; see the items marked PARTIAL and the "Not validated" list.
Screenshots in [docs/img](img/) are from runs A and B. Run C's screenshots show the owner's desktop and
are not committed; its numbers below are read from them and from pasted terminal output.

## Environment (runs A and B, x86_64)

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

## Run C: Raspberry Pi 5 (stack and both agents on the same Pi)

| Item | Value |
|---|---|
| Date / time | 2026-11-01, about 11:03 to 11:48 UTC (the screenshots show IST, UTC+5:30) |
| Run by | The Pi's owner, over SSH from a Windows laptop, following commands given by an AI |
| Commit | `6665851` |
| Host | Raspberry Pi 5 Model B Rev 1.1, 8 GB RAM, aarch64, booting from SD card, 90 GB free |
| OS / Docker / Compose / Python | Debian GNU/Linux 13 (trixie) / 29.4.1 / v5.1.3 / 3.13.5 |
| Network | Phone hotspot (mobile data). Pi and laptop on the same hotspot; dashboard viewed from the laptop at `http://<pi-ip>:3000`. |
| Clean start | Fresh clone and newly created Docker volumes. Other containers already on the Pi were untouched. |

| Check | Result | Evidence |
|---|---|---|
| `git clone -b claude/happy-franklin-k77hxk <url>` | PASS | HEAD `6665851`; no credentials asked (the repo is public). |
| `docker compose up -d` | PASS | Three images pulled over the hotspot: VictoriaMetrics 25 s, collector 104 s, Grafana 202 s. Containers started in about 13 s. |
| Images are arm64 | PASS | `docker image inspect` printed `arm64` for all three. |
| Services healthy | PASS | `docker compose ps` all Up. VictoriaMetrics `OK`; Grafana `"database": "ok"`, 13.3.0. |
| Agents `demo-pi-1`, `demo-pi-2` | PASS | Both ran from a venv, so `pip install -r agent/requirements.txt` worked on aarch64 with Python 3.13. The install output and the `--dry-run` output were not captured. |
| Real temperature sensor | PASS | Dashboard showed 48.0 °C, later 46.3 to 46.9 °C, for both devices. At the same time CPU about 1.5%, memory 16.9%, disk 22.9%, uptime 46 min. |
| Dashboard from another machine | PASS | The laptop's browser showed both devices ONLINE (2 online, 0 offline). |
| Offline detection | PASS | Ctrl+C on `demo-pi-2`: OFFLINE after **38 s** (owner's stopwatch). Its last seen was 17:14:16; `demo-pi-1` stayed ONLINE. |
| Alert | PASS | `DeviceOffline` Alerting for `demo-pi-2` since 17:15:30, which is **74 s** after its last sample (Instances tab); `demo-pi-1` Normal. Destination `empty`: no contact point, as documented. |
| Recovery | PARTIAL | Dashboard back to ONLINE **within at most 17 s** of restarting the agent (the owner came back after 17 s and it was already green; exact time not measured). The alert instance returning to Normal was **not observed** on the Pi. |

Footprint. Server: 587 MB download for the three arm64 images (from registry metadata) and about
2.6 GB on disk (x86 image sizes). Each device: about 8 MB of aarch64 wheels to download, about 28 MB
installed, about 43 MB RAM resident (measured on x86, not on a Pi). The 587 MB is paid once, on the
server, not per device.

## Run D: the `./fleet` helper (x86_64, real Docker stack, run by an AI agent)

Tested against the real containers from empty volumes, not only against fakes. Images were already
cached on this machine, so no large download happened. **Not run on the Raspberry Pi.**

| Check | Result | Evidence |
|---|---|---|
| `./fleet up` from empty volumes | PASS | Three services reported ready, then the dashboard URL, login and device address were printed. 67 s cold (including Grafana's first start), 3 s when re-run on existing volumes. |
| `./fleet add <id>` waits for a device | PASS | Started in the background, then the agent was started: detected as online 16 s after the agent started, exit 0. |
| `./fleet list`, `check`, `status`, `list --json` | PASS | Correct states and readings for two live devices; `status` exit 0 when healthy, 1 when not. |
| Offline detection from the CLI | PASS | `demo-pi-2` stopped: OFFLINE in `./fleet list` after 38 s, the same rule as the dashboard. After a minute its readings show `-`, like the dashboard's N/A. |
| `./fleet remove` refuses a live device | PASS | `remove demo-pi-1 --yes` while it reported: error, exit 1, nothing deleted. |
| `./fleet remove` on an offline device | PASS | 0 series left for that device (both selectors), the other device's 4 series untouched, gone from `list`. Its firing alert disappeared within 20 s (sampled every 10 s). |
| `./fleet down`, answer `n` | PASS | "Aborted. Nothing was changed." exit 1, stack still up. Run in a real pseudo-terminal. |
| `./fleet down`, answer `y` | PASS | Warned that 1 device was online, stopped, "3 container(s) removed", 0 containers left, both data volumes kept. |
| `./fleet down` twice | PASS | Second run: "Nothing to stop", exit 0. |
| `./fleet down --wipe` | PASS | Wrong confirmation word aborted with volumes intact. Right word deleted both volumes (0 left) and all containers. |
| No terminal and no `--yes` | PASS | Refuses with "re-run with --yes" instead of hanging. |
| Data survives down and up | PASS | The device and its history were still there after `./fleet up`. |
| Running agent resumes by itself | PASS, short outage only | A new sample arrived after the stack returned, with no agent restart; its log showed `UNAVAILABLE, retrying` then recovery. The outage was roughly half a minute; a long outage was not tested. |
| First-run download notice | PARTIAL | The missing-image detection was tested for real (it reports an absent image) but the notice was not seen end to end, since the images were cached. |
| Unit tests | PASS | `tests/test_fleet.py`: 32 tests with a fake VictoriaMetrics server and patched Docker calls; with the 33 agent tests, 65 pass. |

Bugs the live run found that the fake-server tests missed, all fixed: stray spaces and a `\r` written
into non-terminal output, a trailing space after the last table column, and "not answering" printed for
services that did not exist.

## Run E: `./fleet notify` (x86_64, real Grafana 13.3.0, run by an AI agent)

Tested against the real Grafana, with a small local web server standing in for the destination.
**Not tested against the real Discord, Slack or Telegram services, and not on the Raspberry Pi.**

| Check | Result | Evidence |
|---|---|---|
| `notify set webhook` | PASS | Contact point and routing created, test message accepted, and the listener received it at the right path. |
| A real alert, end to end | PASS | Device stopped: a **firing** message arrived **79 s** later (device id and "has not reported for 64 s" in it). Device restarted: a **resolved** message arrived **59 s** later. `notify status` then reported `Last delivery ok` with the real time. |
| Secret handling | PASS | A URL with a secret in its path appeared 0 times in `fleet`'s output or anywhere in the repo; `status` shows scheme and host only. `test` re-uses the secret Grafana saved: a Slack test reached the secret path without it being sent again. |
| Slack and Discord | PARTIAL | Changing destination type (the update path) works, and each message reached a local listener in that service's own format (Slack: `attachments`, `username`; Discord: `content`, `embeds`, `username`). The real services were not contacted. |
| Telegram | PARTIAL | Settings accepted. Delivery could not be tested: this machine cannot resolve `api.telegram.org`. The failure was reported clearly (exit 1), and Grafana redacted the token in the error. |
| Switch-over delay | FINDING | Grafana saves a change at once but applies it on a **one-minute cycle** (57.8, 59.9 and 59.9 s measured). Until then alerts use the old setting, and its test already passes. `set` now waits for Grafana's applied-config history to gain a new entry; when it returned, the running config matched. `--no-wait` says the old setting is still live, and `status` shows "Change not live yet". |
| Guard for a customised policy | PASS | A policy with its own routes was refused and left untouched; `--force` replaced it. |
| `notify off` | PASS | In a real terminal: `n` aborted; `y` removed the contact point and restored Grafana's default routing; a second `off` was harmless. |
| Settings across `down` and `up` | PASS | Still configured and live right after a restart. `--wipe` deletes them, and its warning says so. |
| Wrong Grafana password | PASS | Clear advice (`FLEET_GRAFANA_AUTH`), exit 1. |
| Unit tests | PASS | `tests/test_fleet_notify.py`: 34 tests against a fake Grafana whose behaviour was copied from the real one; 99 tests pass in total. |

Bugs the work found, all fixed: `set` said "Alerts now go to X" before Grafana had switched over;
`./fleet ... | head` printed a `BrokenPipeError` traceback; and a test fixture that made one test wait out
a 100 s timeout.

## Run F: start at boot, `agent/install-service.sh` (real systemd 252 in a container, x86_64)

Tested under a **real systemd** (Debian 12, systemd 252) running as PID 1 in a privileged container that
shared the host's network, reporting to the real stack on the host. **This is not a Raspberry Pi and not a
real power cycle.** A "reboot" here is a container restart: systemd stops and boots again, and the files
stay.

| Check | Result | Evidence |
|---|---|---|
| Install on a device that never had the agent | PASS | 18.6 s including downloading the two Python packages. The service was `active` and `enabled`, running as a throwaway unprivileged user (`device-agent`), not root. The device appeared on the stack. |
| Crash recovery | PASS | `kill -9` on the agent: the journal showed `status=9/KILL` and "Scheduled restart job"; a new process started **5.3 s** later, `NRestarts` went 0 to 1, and the device was ONLINE again. (My first attempt did not actually kill anything because the container had no `kill` binary, and my script wrongly reported success; it was redone properly.) |
| Start at boot | PASS | After restarting the container, systemd came up in about 11 s with the service already `enabled` and `active` (new process id), and a **new sample reached the stack 12 s after the restart**, with nobody starting the agent. |
| The unit is valid | PASS | `systemd-analyze verify`: no problems. |
| Sandboxing | PASS, tightened | The first sandbox scored **8.1 EXPOSED** in `systemd-analyze security`. I added options that are safe for an agent that only reads `/proc` and `/sys` (no capabilities, read-only kernel settings, no namespaces or realtime, and others). It now scores **3.9 OK**. Syscall filters and memory restrictions were left out on purpose because they can break Python extensions. |
| The sandbox still allows everything the agent needs | PASS | All five metrics reached the stack from the sandboxed service, including temperature from a fake sensor (51.234 became 51.23). The sensor was a directory bind-mounted over `/sys/class/thermal`, not real sysfs. Crash recovery was re-tested after tightening (pid 290 to 317). |
| Re-running to change a setting | PASS | `--interval 2`: the agent restarted and the journal showed `interval=2s`. |
| Re-running from the install folder itself | PASS | Found in self-review: copying a file onto itself would have failed. Fixed; re-running `/opt/device-agent/install-service.sh --interval 3` worked and the agent picked up `PUSH_INTERVAL=3`. |
| A failing `systemctl enable` is not hidden | PASS | Found in self-review: the error was swallowed, so a device could look installed but not start at boot. With a stand-in `systemctl` whose `enable` fails, the installer now stops with `error: could not enable device-agent to start at boot: <the reason>` and exit 1. A normal install stays quiet. (My first attempt at this test silently tested nothing, because the stand-in was not picked up; it was redone with a proper file.) |
| Refuses a normal user | PASS | "run with sudo", exit 1, the running service untouched. |
| Missing agent files | PASS | A clear error naming the folder, exit 1, the running service untouched. |
| `--uninstall` | PASS | Service stopped and disabled, process gone, `/opt/device-agent`, the env file and the unit removed. A second uninstall was harmless. About 40 s later the stack showed the device OFFLINE. |
| Input validation | PASS | 16 tests (15 run as root, 1 skipped) feed the script bad IDs, endpoints, intervals and paths, including values that try to inject extra lines into the env file; all are rejected before anything is written. One test checks the script's ID rule against the agent's own. Shellcheck is clean. With the earlier tests, 115 tests run in about 8 s: 114 pass and 1 is skipped when run as root (it needs a normal user, which I ran by hand: it refused with "run with sudo"). |

Noise in the test bed only: the container borrowed the host's Python, which printed a harmless
`_distutils_hack` warning. A real device would not.

## Definition of Done

| # | Item | Result | Evidence |
|---|---|---|---|
| 1 | Stack starts with the documented command | PASS | `docker compose ps`: all three services running on the pinned images. VictoriaMetrics `/health` OK, Grafana `/api/health` database ok (both also `PASS` lines in `validate.sh`). Same on a Raspberry Pi 5 with arm64 images (run C). |
| 2 | Agent sends telemetry | PASS | VictoriaMetrics held `device_cpu_/memory_/disk_utilization_percent` and `device_uptime_seconds` for both devices. Agent unit tests: 33 passed. Agent also ran against the declared minimum OTel version (1.25.0), and on aarch64 with Python 3.13 (run C). |
| 3 | Two distinct devices on one dashboard | PASS | Two rows, `demo-pi-1` and `demo-pi-2` ([01](img/01-two-devices-online-fresh-clone.png)). Series have `instance` equal to `device_id` and `job="device-agent"`. Same on the Pi, viewed from a laptop over the network (run C). |
| 4 | Health metrics visible | PASS | CPU, memory, disk, uptime visible for each device. Temperature: on a **real Pi 5 sensor** it showed 48.0 °C, later 46.3 to 46.9 °C (run C). The x86 VM has no sensor, so there it was checked with the real agent code reading a **fake** sysfs directory: 52.5 °C for that device and "N/A" for the sensorless ones ([04](img/04-temperature-mixed-fleet.png)). The temperature column is absent when no device reports one. |
| 5 | Last-seen updates | PASS | The same online device showed last seen 06:22:44, then 06:24:49, then 06:26:19 in three screenshots taken minutes apart. |
| 6 | Offline is distinguishable | PASS | `demo-pi-2` turned red OFFLINE with last seen equal to the stop time while `demo-pi-1` stayed green ([02](img/02-demo-pi-2-offline.png)). Measured 36.2 s (manual) and 36 s (`validate.sh`) on x86; 38 s on the Pi 5 (run C). |
| 7 | Alert works | PASS | `DeviceOffline` (Provisioned) Firing for the silent device only; `demo-pi-1` Normal ([03](img/03-alert-firing-for-silent-device.png)). Measured 69 s (manual), 71 s (`validate.sh`); Pending seen on the way. Recovery to Normal 12.1 s after restart. On the Pi 5 (run C): Alerting 74 s after the device's last sample, dashboard back to ONLINE within 17 s of restarting; the alert returning to Normal was not observed there. By default no notification is sent; `./fleet notify` adds one (run E). |
| 8 | Quickstart tested | PARTIAL | The README commands were executed verbatim in a fresh clone by an agent (run B), and by a person on a Pi 5 (run C) who reached the firing alert. The person had an AI giving the steps one at a time, so a cold read of the README alone is still untested. The clones used `-b claude/happy-franklin-k77hxk` because the work was not yet in `main`. **That is no longer needed: since PR #1 was merged, a plain `git clone` of `main` (the README's first command) has everything, and its 99 tests pass (checked).** |
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
| Agent on a remote device reached `<server-ip>:4317` | NOT RUN | No separate device pushed over the network. In run C the agents ran on the same Pi as the server. (A laptop did reach the Pi's Grafana on port 3000; port 4317 from another machine was not tested.) |
| It appears as a separate row with its own device ID | NOT RUN | As above. |

## Logs

| Source | Finding |
|---|---|
| otel-collector | No errors. |
| VictoriaMetrics | No errors. One INFO line that cgroup pressure metrics are unavailable (container quirk). |
| Grafana | Error-level lines are all its background plugin installer failing to resolve `grafana.com`; this sandbox blocks that host. Harmless for the demo. INFO-level "database is locked, retrying" on first boot. No provisioning errors. |
| Agents | Clean start and clean shutdown. |
| Run C (Pi 5) | Container and agent logs were not collected. |

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
| 9 | The README's plain `git clone` fetched `main`, which did not contain this work until the branch was merged. | Medium | **Fixed** by merging PR #1 (squash commit `3b509f7`). Checked afterwards: a plain clone of `main` has `fleet`, the agent, the dashboard and the pinned images, and all 99 tests pass from it. |
| 10 | Grafana asks for a new password on first login; one user changed it and then could not log in. | Low | Fixed in the README (troubleshooting entry with the reset command). The command was tested on Grafana 13.3.0 in a container, and the owner then logged in on the Pi. |
| 11 | If the server's IP changes (DHCP, or a phone hotspot that picks a new subnet), devices silently stop reporting. | Medium | Documented in the README. No code change; a reservation or hostname is the fix. |
| 12 | The agents ran in SSH sessions, so closing the session stops them and the alert then fires. No start-at-boot setup was provided. | Low | Fixed by `agent/install-service.sh` (run F). Tested under systemd in a container on x86, not yet on a Pi. |
| 13 | Grafana applies a changed notification setting only on a one-minute cycle, so right after a change alerts still use the old one. | Medium | Measured (about 58 to 60 s). `fleet notify set` waits for the switch-over, `--no-wait` and `status` say so, and the README explains it. |

## Not validated

- A separate device pushing to the server over the network (port 4317 from another machine), or any firewall behaviour.
- 32-bit Raspberry Pi OS, Pi Zero-class hardware, and ESP32-class devices (the agent is Linux-only; an HTTP path to port 4318 worked in a `curl` test only).
- `./fleet` on the Raspberry Pi, `./fleet remove --force` on a live device (unit-tested only), and `./fleet add` driving a real remote device.
- Agents resuming after a long outage of the stack (only about half a minute was tested).
- A person following the README cold, without step-by-step help.
- The alert returning to Normal on the Pi (seen only on x86).
- Notifications reaching the real Discord, Slack or Telegram services, email, repeat messages after the 4-hour repeat interval, flapping devices, and `notify` on the Raspberry Pi.
- Behaviour with more than a handful of devices, or long retention.
- `install-service.sh` on a Raspberry Pi or any real device, a real power cycle, systemd older than 247 (the `ProtectProc` option would be ignored with a warning; not tried), other distributions, and a device with no internet (the installer needs it once).
