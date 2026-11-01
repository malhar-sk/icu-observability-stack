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
- Python 3.9 or newer with `venv` (on Debian / Raspberry Pi OS: `sudo apt install python3-venv`)
- Free ports on the machine that runs the stack:

| Port | Used by |
|---|---|
| 3000 | Grafana (dashboard, login `admin` / `admin`) |
| 4317 | OpenTelemetry Collector, OTLP gRPC (what the agents send to) |
| 4318 | OpenTelemetry Collector, OTLP HTTP (published, not used by the agent) |
| 8428 | VictoriaMetrics (storage and query API) |

### 1. Launch the stack and install the agent

```bash
git clone https://github.com/malhar-sk/icu-observability-stack.git
cd icu-observability-stack

docker compose up -d

python3 -m venv .venv && . .venv/bin/activate && pip install -r agent/requirements.txt
```

### 2. Run two devices (two terminals)

Both terminals need the venv (`. .venv/bin/activate`) and must be in the repository directory.

```bash
# terminal 1
python agent/device_agent.py --device-id demo-pi-1

# terminal 2
python agent/device_agent.py --device-id demo-pi-2
```

Each agent logs a few startup lines and then runs quietly, pushing every 5 seconds.
Both demo "devices" are the same machine here; what tells them apart is the `--device-id`.

### 3. Open the dashboard

Go to <http://localhost:3000> and log in with `admin` / `admin` (Grafana may ask you to change the
password; you can skip that). The **Device Fleet** dashboard opens as the home page. Within about
15 to 20 seconds of starting the agents you should see one row per device: device ID, hostname,
state (ONLINE / OFFLINE), last seen, CPU %, memory %, disk % and uptime. A **temperature** column
appears as soon as any device reports one (a Raspberry Pi does, via `/sys/class/thermal`); devices
without a sensor then show "N/A" in it. If no device has a sensor, as on many VMs, the column is
simply not shown.

### 4. Simulate a device going offline

Press **Ctrl+C** in the `demo-pi-2` terminal.

- **Within roughly 30 to 40 seconds** the `demo-pi-2` row switches to **OFFLINE** (a device is
  offline when it has not reported for more than 30 s). `demo-pi-1` stays ONLINE.
- In Grafana go to **Alerting, then Alert rules**, expand the folder **Device Fleet** and the group
  `device-health`, and click **DeviceOffline**. The **Instances** tab (and the Threshold table at the
  bottom of the page) lists one instance per device. `demo-pi-2` goes **Pending** and then **Firing**
  about 70 seconds after you stopped the agent; `demo-pi-1` stays **Normal**.
- Start `demo-pi-2` again: it returns to ONLINE and the alert instance goes back to Normal
  within roughly 20 s (the rule is evaluated every 10 s).

### 5. Stop the stack

```bash
docker compose down        # stops the containers, keeps stored metrics and Grafana state
docker compose down -v     # ALSO DELETES all stored metrics and Grafana state
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

1. On the server, run `docker compose up -d` and find its IP address (for example `192.168.1.10`).
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
Keeping the agent running across reboots (for example as a service) is up to you and is not covered here.

> **Trusted networks only.** Port 4317 has **no authentication and no TLS**. Anyone who can reach it
> can write metrics under any device name. Use it on a private LAN or VPN, never on the open internet.
> The Grafana login is the default `admin` / `admin`.

---

## Troubleshooting

**The agent runs but my device does not appear.**
Wait about 10 seconds first (VictoriaMetrics is configured to hide the newest 5 s of data, and the
agent pushes every 5 s). Then check, in this order:

```bash
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
per-device state (Normal / Pending / Firing). No notifications (email, chat) are configured; the
alert is only visible in Grafana.

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
- **No device registry.** A device that has never reported is invisible, and a device silent for more than a day drops off the dashboard.
- **No notifications.** No contact point is configured, so an alert is visible only inside Grafana (and its API).
- **Offline detection is not instant.** It needs a push every 5 s and takes about a minute from the device stopping to the alert firing.
- **Temperature** is reported only where `/sys/class/thermal` provides it.
- **Single node, demo scale.** One VictoriaMetrics, one Collector, one Grafana, no redundancy or retention tuning.
- **Verified on x86_64 Linux only** so far; whether the pinned images and the agent behave the same on a Raspberry Pi (aarch64) has not been checked by this repository's validation.
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
