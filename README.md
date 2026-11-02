# Multi-device health monitoring (Raspberry Pi / Linux)

> **Demonstration only.** This is a small proof of concept, not production-ready software,
> and it is **not suitable for clinical or medical-device use**. It has no authentication
> and no encryption (see [Known limitations](#known-limitations)).

## Quickstart

### What this is

A small open-source stack for **independent IoT developers, researchers and small teams** who run
a handful of Raspberry Pi or Linux devices. Each device runs a tiny Python agent that pushes its
health (CPU, memory, disk, uptime and, where available, temperature) to a central server. You
watch **every device's health and connectivity on one Grafana dashboard** and get an alert when a
device stops reporting. The server side is plain Docker Compose: OpenTelemetry Collector,
VictoriaMetrics and Grafana.

### Prerequisites

- Docker with Compose v2 (`docker compose version` must work) and permission to run it
- Python 3.9 or newer with `venv` (runs both `./fleet` and the device agent; on Debian / Raspberry Pi OS: `sudo apt install python3-venv`)
- Free ports on the machine that runs the stack:

| Port | Used by |
|---|---|
| 3000 | Grafana (dashboard, login `admin` / `admin`) |
| 4317 | OpenTelemetry Collector, OTLP gRPC (what the agents send to) |
| 4318 | OpenTelemetry Collector, OTLP HTTP (published, not used by the agent) |
| 8428 | VictoriaMetrics (storage and query API) |

### Commands at a glance

One helper, `./fleet`, runs everything below. Use it on the machine that hosts the stack:

| Command | What it does |
|---|---|
| `./fleet up` | Start everything, wait until it is ready, print the dashboard address |
| `./fleet down` | Stop everything. **Asks first** and confirms afterwards. `--wipe` also deletes stored data |
| `./fleet status` | Is the stack healthy, and how many devices are online |
| `./fleet list` | Every device: state, last seen, CPU, memory, disk, temperature, uptime (`--json` for scripts) |
| `./fleet check <id>` | One device in detail |
| `./fleet add <id>` | Onboard a device: prints what to run on it and waits until it appears |
| `./fleet remove <id>` | Forget a retired device (deletes its stored data, asks first) |
| `./fleet notify set\|test\|status\|off` | Send alerts to Discord, Slack, Telegram or a webhook |

It is a thin wrapper around `docker compose` and the stack's own APIs, so the plain commands still
work; they are listed [at the end of this section](#without-the-helper).

### 1. Start the stack

```bash
git clone https://github.com/malhar-sk/icu-observability-stack.git
cd icu-observability-stack

./fleet up
```

`./fleet up` starts the three services, waits until each answers, then prints the dashboard address,
the login, and the address devices report to. The **first run downloads about 590 MB** of Docker
images (mostly Grafana), so avoid a metered connection such as a phone hotspot.

### 2. Run two devices (two terminals)

Both "devices" are this machine here; what tells them apart is the `--device-id`. In the repository
folder, once:

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r agent/requirements.txt
```

Then in two terminals (each needs `. .venv/bin/activate` first):

```bash
# terminal 1
python agent/device_agent.py --device-id demo-pi-1

# terminal 2
python agent/device_agent.py --device-id demo-pi-2
```

Each agent logs a few startup lines and then runs quietly, pushing every 5 seconds.

**For a real, separate device use `./fleet add kitchen-pi` instead.** It checks the ID is free, prints
the exact copy-and-run commands with this machine's address filled in, and waits until the device
shows up. See [Monitoring a real remote Raspberry Pi](#monitoring-a-real-remote-raspberry-pi).

### 3. Open the dashboard

Go to <http://localhost:3000> and log in with `admin` / `admin` (Grafana may ask you to change the
password; you can skip that). The **Device Fleet** dashboard opens as the home page. Within about
15 to 20 seconds of starting the agents you should see one row per device: device ID, hostname,
state (ONLINE / OFFLINE), last seen, CPU %, memory %, disk % and uptime. A **temperature** column
appears as soon as any device reports one (a Raspberry Pi does, via `/sys/class/thermal`); devices
without a sensor then show "N/A" in it. If no device has a sensor, as on many VMs, the column is
simply not shown. The graphs below the table need a little longer than the table to fill in (they
showed "No data" 17 s after start and had lines about 30 s in).

### 4. Check on your devices

```bash
./fleet list              # state, last seen and readings for every device
./fleet check demo-pi-2   # one device in detail
```

### 5. Simulate a device going offline

Press **Ctrl+C** in the `demo-pi-2` terminal.

- **Within roughly 30 to 40 seconds** the `demo-pi-2` row switches to **OFFLINE** (a device is
  offline when it has not reported for more than 30 s), and `./fleet list` shows the same.
  `demo-pi-1` stays ONLINE.
- In Grafana go to **Alerting, then Alert rules**, expand the folder **Device Fleet** and the group
  `device-health`, and click **DeviceOffline**. The **Instances** tab (and the Threshold table at the
  bottom of the page) lists one instance per device. `demo-pi-2` goes **Pending** and then **Firing**
  about 70 seconds after you stopped the agent; `demo-pi-1` stays **Normal**.
- Start `demo-pi-2` again: it returns to ONLINE and the alert instance goes back to Normal
  within roughly 20 s (the rule is evaluated every 10 s).

### Alerts that reach you (optional)

Out of the box, an offline device only shows up if you open Grafana. To get a message instead:

```bash
./fleet notify set discord                   # asks for the webhook URL, with the input hidden
./fleet notify set slack https://hooks.slack.com/services/...
./fleet notify set telegram <bot-token> <chat-id>
./fleet notify set webhook https://your.service/endpoint   # anything that accepts a JSON POST
```

`set` hands the address to Grafana (which stores it encrypted; `fleet` never keeps it), sends a test
message, and then **waits until Grafana has switched over**. That takes up to about a minute, because
Grafana applies alerting changes on a one-minute cycle; until then alerts still use the old setting.
After that:

```bash
./fleet notify test      # send another test message
./fleet notify status    # where alerts go, and whether the last real delivery worked
./fleet notify off       # stop sending (alerts stay visible in Grafana)
```

You get one message per device when it goes offline and a recovery message when it is back. In testing
the offline message arrived about 80 s after the device stopped, and the recovery message about a
minute after it returned. If you already customised Grafana's notification policy, `set` refuses to
overwrite it unless you add `--force`.

**What was tested:** a real alert delivered end to end to a webhook, and the message formats for Slack
and Discord checked against a local listener, not the real services. Telegram's delivery could not be
tested (the test machine could not reach it); its failure message was. Email is not covered by this
command. See [docs/VALIDATION.md](docs/VALIDATION.md), Run E.

### 6. Retire a device

A device that is gone for good stays on the dashboard as OFFLINE for a day, and keeps its alert. Stop
its agent, then:

```bash
./fleet remove demo-pi-2
```

It asks first, then deletes that device's stored data. The device is gone from the dashboard table
and `./fleet list` at once, and its alert cleared in under 20 s in testing. `remove` refuses a device
that is reporting right now unless you add `--force` (it comes back on its next report).

### 7. Stop the stack

```bash
./fleet down            # asks first; stops everything, keeps stored metrics and Grafana state
./fleet down --wipe     # ALSO DELETES all stored metrics and Grafana state (asks twice)
```

Devices keep running and retry while the stack is down. After a short outage (about half a minute,
the only length tested) they resumed by themselves once `./fleet up` was back.

### Without the helper

`./fleet` only wraps these, so you can always run them directly:

```bash
docker compose up -d       # = ./fleet up, without waiting for readiness
docker compose down        # = ./fleet down, without asking
docker compose down -v     # = ./fleet down --wipe, without asking: ALSO DELETES stored data
```

### Check everything automatically

`scripts/validate.sh` runs the whole scenario above without a browser: it starts the stack, runs two
agents, checks both devices appear, stops one, and waits for it to be OFFLINE and for the
`DeviceOffline` alert to fire for that device only. It takes about three minutes, exits non-zero on any
failure, and leaves the stack running unless you pass `--down`.

```bash
scripts/validate.sh          # uses .venv/bin/python automatically if it exists
scripts/validate.sh --help   # environment overrides (VM_URL, GRAFANA_URL, GRAFANA_AUTH, PYTHON, ...)
```

It uses the device IDs `demo-pi-1` and `demo-pi-2`, so stop your own agents with those IDs first.
Record results in [docs/VALIDATION.md](docs/VALIDATION.md).

---

## Configuration

The agent reads flags first, then environment variables, then defaults.

| Setting | Flag | Env var | Default |
|---|---|---|---|
| Device ID (unique, stable across restarts; `^[a-z0-9][a-z0-9._-]{0,62}$`) | `--device-id` | `DEVICE_ID` | lowercased hostname |
| Collector address, plaintext gRPC `host:port` (a leading `http://` is stripped) | `--endpoint` | `OTLP_ENDPOINT` | `localhost:4317` |
| Push interval in seconds | `--interval` | `PUSH_INTERVAL` | `5` |
| Path whose disk usage is reported | `--disk-path` | `DISK_PATH` | `/` |
| Print one JSON snapshot and exit, sending nothing | `--dry-run` | | off |

Try `python agent/device_agent.py --dry-run` first to see what a device would report. An example
environment file is in `agent/device-agent.env.example` (load it with
`set -a; . agent/device-agent.env.example; set +a`).

Metrics pushed (all gauges, each labelled `device_id` and `hostname`): `device_cpu_utilization_percent`,
`device_memory_utilization_percent`, `device_disk_utilization_percent`, `device_uptime_seconds` and
`device_temperature_celsius`. A sensor that cannot be read is simply not reported (never sent as 0).
`device_uptime_seconds` is always sent and is what defines "last seen". The full contract is in
[docs/CONTRACT.md](docs/CONTRACT.md).

### Monitoring a real remote Raspberry Pi

The shortcut: on the server run `./fleet add garage-pi`. It checks the ID is free, prints the commands
below with the server's address filled in, and waits until the device appears. The manual steps:

1. On the server, run `./fleet up` and find its IP address (for example `192.168.1.10`).
2. Make sure the Pi can reach the server on **TCP 4317** (check any firewall). From the Pi, this
   prints `reachable` or fails with an error:
   `python3 -c "import socket; socket.create_connection(('192.168.1.10', 4317), 3); print('reachable')"`
3. Copy `agent/device_agent.py` and `agent/requirements.txt` to the Pi, create a venv and install
   the requirements (same commands as the quickstart).
4. Run it with an endpoint and a **distinct, stable** device ID:

```bash
python agent/device_agent.py --device-id garage-pi --endpoint 192.168.1.10:4317
# or: DEVICE_ID=garage-pi OTLP_ENDPOINT=192.168.1.10:4317 python agent/device_agent.py
```

Two devices that use the same `--device-id` overwrite each other's data, so give every device its own.
**Keep it running across reboots.** Running the agent by hand stops when you close the terminal or the
device restarts. To make it a service that starts at boot and restarts itself if it crashes, copy
`agent/install-service.sh` over together with the agent (`./fleet add` shows the `scp` line) and, on the
device:

```bash
sudo bash install-service.sh --device-id garage-pi --endpoint 192.168.1.10:4317
```

It needs systemd and internet access once (to install the agent's two Python packages), puts everything
under `/opt/device-agent` with its own Python environment, and runs the agent as a throwaway
unprivileged user with a restricted sandbox. Re-run it to change a setting; undo it with
`sudo bash install-service.sh --uninstall`. Add `--print` to see exactly what it would write without
changing anything. Check on it with `systemctl status device-agent` and `journalctl -u device-agent -f`.
Tested under a real systemd in a container (start, crash recovery, reboot, uninstall); **not yet on a
Raspberry Pi**.

**Give the server a stable address.** Devices find it by the `--endpoint` you give them. If the
server's IP can change (ordinary DHCP, or a phone hotspot, which picks a new subnet from time to
time), devices will silently stop reporting. Reserve the server's address in your router, or use a
hostname you know resolves from your devices. Do not set a fixed IP on a phone-hotspot Wi-Fi profile:
the subnet changes and the device ends up unreachable.

> **Trusted networks only.** Port 4317 has **no authentication and no TLS**. Anyone who can reach it
> can write metrics under any device name. Use it on a private LAN or VPN, never on the open internet.
> The Grafana login is the default `admin` / `admin`.

---

## Troubleshooting

**The agent runs but my device does not appear.**
Wait about 10 seconds first (VictoriaMetrics is configured to hide the newest 5 s of data, and the
agent pushes every 5 s). Then check, in this order:

```bash
./fleet status                             # one-glance health of the stack, plus how many devices are online
docker compose ps                          # victoriametrics, otel-collector and grafana should all be "Up"
curl localhost:8428/health                 # prints OK
docker compose logs otel-collector         # look for errors exporting to VictoriaMetrics
curl -s 'localhost:8428/api/v1/query?query=device_uptime_seconds'   # your device_id should be in the result
```

On a remote device, also check that the `--endpoint` is `host:port` (not `https://`), that the server
IP is right, and that TCP 4317 is reachable (the Python one-liner in the remote-device steps above).

**The dashboard is empty or shows "No data".**
Confirm the query above returns your device. Then in Grafana open Connections, Data sources,
*VictoriaMetrics*, and press **Test**. Devices that have not reported at all in the last day are not
shown, and a device that has never reported is invisible (there is no device registry).

**The alert does not fire.**
It needs the device to have reported at least once and then be silent for more than 30 s, plus a
30 s pending period; allow about a minute after stopping the agent. A device with no data at all
does not raise an alert. Look under Alerting, Alert rules, *Device Fleet*, `DeviceOffline` for the
per-device state (Normal / Pending / Firing). Unless you ran `./fleet notify set ...`, no message is
sent anywhere; the alert is only visible in Grafana.

**A port is already in use (`docker compose up` fails).**
Find the owner with `ss -ltnp | grep -E ':(3000|4317|4318|8428)\b'` and stop it, or change the left
side of the matching `ports:` entry in `docker-compose.yml` (for example `"3001:3000"`). If you move
4317, change the agents' `--endpoint` to match.

**`pip install` says "externally-managed-environment", or `python3 -m venv` fails.**
Use the venv from the quickstart rather than installing into the system Python. On Debian / Raspberry
Pi OS install `python3-venv` first.

**Start again from a clean state.**
`docker compose down -v` removes the containers **and the stored metrics and Grafana state**
(the dashboard and alert rule are provisioned from files, so they come back on the next `up`).
Then `docker compose up -d`. Without `-v`, old devices stay on the dashboard for a day.

**Grafana reports a conflict with the data source "VictoriaMetrics".**
The stack provisions a data source with that name. If your Grafana volume already holds one that you
created by hand, delete that one in Grafana (Connections, Data sources) or do a clean reset as above.

**I forgot the Grafana password** (for example you changed it when Grafana asked on first login).
Reset it to `admin` without touching your dashboard, alert rule or data:

```bash
docker compose exec grafana grafana cli admin reset-admin-password admin
```

If your browser still rejects `admin` / `admin`, it may be auto-filling an old saved password; try a
private window.

---

## How it works

```
 device_agent.py  (one per device, every 5 s)
        |  OTLP / gRPC, plaintext
        v
 OpenTelemetry Collector     :4317 (gRPC)  :4318 (HTTP)
        |  Prometheus remote write
        v
 VictoriaMetrics :8428  <------ Grafana :3000
                                 - dashboard "Device Fleet"
                                 - alert rule "DeviceOffline"
```

Each agent samples `/proc` and `/sys` and pushes five gauges, tagged with its `device_id`. The
Collector (`otel-collector.yaml`) forwards them unchanged to VictoriaMetrics. Grafana is provisioned
from files in `grafana/` (data source, dashboard and alert rule), so nothing is set up by hand.
A device's **last seen** time is the newest `device_uptime_seconds` sample, its **age** is
`time() - tlast_over_time(device_uptime_seconds[1d])`, and it is **OFFLINE** when the age is above
30 s (six missed pushes). The `DeviceOffline` rule uses the same query, evaluated every 10 s, with
a 30 s pending period, and produces one alert instance per device.

Vector (`vector.toml`) is not part of the default stack. It is parked behind the compose profile
`legacy-vector` because its demo configuration is broken; do not start it expecting it to work.

## Known limitations

- **No authentication or TLS** anywhere: agents send plaintext gRPC and anyone who can reach port 4317 can inject data. Grafana uses `admin` / `admin`.
- **No device registry.** A device that has never reported is invisible, and a device silent for more than a day drops off the dashboard. That is why `./fleet add` cannot "create" a device on the server: a device exists once it reports.
- **`./fleet` runs on the server only.** It prints the commands for a device but cannot install the agent there; on a device, `agent/install-service.sh` makes the agent start at boot (systemd only). Both `./fleet` and the installer were tested on x86_64 Linux; neither has been run on the Raspberry Pi.
- **Notifications are opt-in.** Until you run `./fleet notify set ...`, an alert is visible only inside Grafana (and its API). `fleet notify` covers Discord, Slack, Telegram and webhooks, not email, and only the webhook path was tested end to end.
- **Offline detection is not instant.** It needs a push every 5 s and takes about a minute from the device stopping to the alert firing.
- **Temperature** is reported only where `/sys/class/thermal` provides it.
- **Single node, demo scale.** One VictoriaMetrics, one Collector, one Grafana, no redundancy or retention tuning.
- **Verified on x86_64 Linux and on one Raspberry Pi 5** (aarch64, Debian 13 "trixie"), where the stack and both demo agents ran on the same Pi and the dashboard was viewed from a laptop. **Not yet verified:** a separate device pushing to the server over the network, 32-bit Raspberry Pi OS, Pi Zero-class hardware, and ESP32-class devices (the agent is Linux-only). See [docs/VALIDATION.md](docs/VALIDATION.md).
- **Demonstration only.** Not production-ready and not suitable for clinical or medical-device use.

---

## Original ICU floor simulator demo

This repository began as a simulation of equipment telemetry for a hospital ICU floor. It is a
**simulation with fake numbers**; it does not connect to any real medical equipment, and the
repository's own tooling is not intended for monitoring patient-care systems.

`clinic_simulator.py` is unchanged and still pushes simulated data for 5 beds (`bed_1` to `bed_5`,
`floor="1"`) through the same Collector and VictoriaMetrics, as these gauges:

- `clinic_oxygen_psi` (oxygen supply)
- `clinic_life_support_watts` (life support power)
- `clinic_battery_pct` (battery backup)
- `clinic_device_heartbeat` (1 online, 0 offline)

**Not included in this repository:** the Grafana dashboard that was called "ICU Floor 1" and its
alert rules (including the thresholds in the table below) were created by hand in someone's
Grafana and were never exported here. The stack above provisions only the *Device Fleet*
dashboard and the `DeviceOffline` rule. To see the simulator's data you have to build your own
panels on the `clinic_*` metrics.

The two images below are screenshots of a separate project web page that imitates this demo in the
browser. They are **not** Grafana screenshots and are not produced by this repository's stack.

![Landing page](p1.png)
![Interactive demo page](p2.png)

### Run the simulator

With the stack running, install the two packages into the venv from the quickstart and run a
scenario (it pushes to `localhost:4317`; `normal` runs until you press Ctrl+C, the other
scenarios end on their own):

```bash
. .venv/bin/activate
pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc

python clinic_simulator.py normal                        # all beds nominal
python clinic_simulator.py oxygen_leak --bed bed_1       # PSI drops from 55 towards 5 over ~30 s
python clinic_simulator.py power_failure --bed bed_2     # mains power cuts, battery drains
python clinic_simulator.py device_offline --bed bed_3    # heartbeat goes to 0
```

Run `normal` afterwards to reset all beds to safe values.

For reference, the thresholds the original dashboard used:

| Metric | Safe | Critical |
|---|---|---|
| Oxygen PSI | 30 or more | below 30 |
| Life support (Watts) | 100 or more | below 100 |
| Battery backup (%) | 20 or more | below 20 |
| Device heartbeat | 1 (online) | 0 (offline) |

`send_metrics.py` is a minimal example that pushes one counter every 5 s to the same Collector.

## License

MIT
