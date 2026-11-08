# Multi-device health monitoring (Raspberry Pi / Linux)

> **Demonstration only.** This is a small proof of concept, not production-ready software,
> and it is **not suitable for clinical or medical-device use**. It has no authentication
> and no encryption (see [Known limitations](#known-limitations)).

## Quickstart: from nothing to a dashboard with your devices on it

One command, `./fleet`, runs everything. The whole workflow is: **put the server on a laptop or a Raspberry Pi**
(steps 1 to 3), **add each device the way that suits it** (step 4), then look at the dashboard and set up alerts
(steps 5 and 6).

```
  your devices                                    the server (a laptop or a Raspberry Pi)
  ------------                                    ---------------------------------------
  Raspberry Pi, Linux machine --- agent --------->  one Grafana dashboard: every device, ONLINE / OFFLINE
  ESP32, Pico W, other boards --- MQTT ---------->  an alert when a device goes quiet
  cron job, script, router ------ check-in URL -->  (optionally a message to your phone)
  router, NAS, service ---------- watched from the server, nothing installed on them
```

### 1. Pick the machine for the server

The server collects, stores and shows everything. **It must stay switched on:** while it is off or asleep nothing
is recorded and no alert can fire (devices keep trying and carry on when it is back).

| Server machine | Status |
|---|---|
| A Linux laptop or desktop (x86_64) | **Tested** on Ubuntu 24.04 |
| A Raspberry Pi 5, 64-bit Raspberry Pi OS or Debian | **Tested** (Debian 13, arm64) |
| A Raspberry Pi 4, 64-bit OS | Not tested |
| A Pi Zero 2 W, or any 32-bit OS | Not for the server (too little memory). As a *device* it should work (not tested). |
| macOS or Windows (Docker Desktop) | Not tested |

It needs about **590 MB of downloads and 2.6 GB of disk** (measured on arm64), and about 335 MiB of RAM while running
(measured on x86: Grafana 258, VictoriaMetrics 41, collector 35). Use a connection without a data cap for the first start (not a phone hotspot).

### 2. Install what the server needs

Docker with Compose v2, Python 3.9 or newer, and git. On Debian, Ubuntu or Raspberry Pi OS:

```bash
curl -fsSL https://get.docker.com | sudo sh        # Docker's own installer (docs.docker.com/engine/install has the manual way)
sudo usermod -aG docker $USER                      # then log out and back in, so you can run docker without sudo
sudo apt install -y git python3 python3-venv
docker compose version                             # must print a version
```

The server uses these ports. Nothing else on the machine may use them, and if it has a firewall (`ufw`, say), devices
on other machines must be able to reach the ones they use:

| Port | Used by | Who connects |
|---|---|---|
| 3000 | Grafana, the dashboard | you, in a browser |
| 4317 | OpenTelemetry Collector (what the Linux agent sends to) | Raspberry Pis and Linux machines |
| 8428 | VictoriaMetrics (storage and query API) | the server itself |
| 1883 | MQTT broker, only after `./fleet up --mqtt` | boards that speak MQTT |
| 8081 | Check-in URL, only after `./fleet add <id> --checkin` | anything running `curl` |

### 3. Start the server

```bash
git clone https://github.com/malhar-sk/icu-observability-stack.git
cd icu-observability-stack
./fleet up
```

It starts everything, waits until each part answers, and prints the dashboard address and where devices report to.
Open the dashboard address (<http://localhost:3000> on the server itself) and log in as `admin` with the password it printed.
On a first install `./fleet up` makes a random one and keeps it in `.env` (`./fleet password --show` prints it again;
`./fleet password` changes it). The **Device Fleet** dashboard is the home page, empty for now.

**Give the server a stable address.** Devices find it by its IP address (`./fleet up` and `./fleet add` print it). If the
address changes (ordinary DHCP, or a phone hotspot, which picks a new subnet from time to time) devices silently stop
reporting. Reserve the server's address in your router. Do not set a fixed IP on a phone-hotspot Wi-Fi profile.

### 4. Add your devices

Pick the row that matches the device. Each has its own short walkthrough below.

| The device is... | How it reports | Needs on the device |
|---|---|---|
| A Raspberry Pi or other Linux machine you can log in to | [**4a. The agent**](#4a-a-raspberry-pi-or-linux-machine-the-agent) | Python 3.9+, internet once |
| An ESP32, Pico W, Arduino or other board with Wi-Fi or Ethernet | [**4b. MQTT**](#4b-a-board-that-speaks-mqtt-esp32-pico-w-arduino) | MQTT code in its firmware |
| A cron job, a script, a router, anything that can run `curl` | [**4c. Check-in URL**](#4c-anything-that-can-run-curl-the-check-in-url) | `curl` |
| A router, NAS, web service or ESPHome board you would rather not touch | [**4d. Watch it**](#4d-something-you-cannot-install-anything-on-watch-it) | nothing |

You can mix all four. Every device needs its own **device ID**: lowercase letters, digits, `.`, `_` and `-` (for
example `kitchen-pi`). Two devices with the same ID overwrite each other.

#### 4a. A Raspberry Pi or Linux machine: the agent

The agent is a small Python program that reports CPU, memory, disk, uptime and (where the machine has a sensor)
temperature every 5 seconds. A Pi Zero 2 W follows the same steps (not tested on one).

1. **On the server**, run `./fleet add kitchen-pi`. It checks the ID is free, prints the exact commands below with the
   server's address filled in, and then waits for the device to appear.
2. **Copy the agent to the device** (run this on the server; replace `pi@PI-ADDRESS` with the device's login):
   ```bash
   scp agent/device_agent.py agent/requirements.txt agent/install-service.sh pi@PI-ADDRESS:
   ```
3. **On the device**, try it first (it stops when you close the terminal):
   ```bash
   sudo apt install -y python3-venv                 # Raspberry Pi OS / Debian, if it is missing
   python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
   python device_agent.py --device-id kitchen-pi --endpoint SERVER-ADDRESS:4317
   ```
4. **Keep it running for good** (starts at boot, restarts if it crashes). On the device:
   ```bash
   sudo bash install-service.sh --device-id kitchen-pi --endpoint SERVER-ADDRESS:4317
   ```
   Undo it with `sudo bash install-service.sh --uninstall`. It needs systemd and internet access once.

Within about 15 seconds `./fleet add` prints that the device is online. More detail, and the manual steps:
[Monitoring a real remote Raspberry Pi](#monitoring-a-real-remote-raspberry-pi).

#### 4b. A board that speaks MQTT (ESP32, Pico W, Arduino)

MQTT is the usual language of hobby boards. The server runs the broker for you.

1. **On the server**, turn it on once: `./fleet up --mqtt` (or `./fleet up --mqtt-broker 192.168.1.20 --mqtt-user NAME`
   if you already run a broker). It remembers the choice.
2. **Add the board:** `./fleet add garage-esp32 --mqtt`. It prints the broker's address, user name and password, the
   topics to publish to, and test commands, then waits for the board.
3. **In the board's code**, connect to that broker and publish plain numbers every time it reports:
   - `fleet/garage-esp32/uptime_seconds`: any number that keeps growing (**required**, it is the "alive" signal)
   - `fleet/garage-esp32/report_interval_seconds`: how often it reports, so a board that sleeps for a minute is not
     flagged after 30 s
   - `fleet/garage-esp32/temperature_celsius`, or any other reading name

Examples for shell, Python, MicroPython and Arduino, and what the bridge refuses: **[docs/MQTT.md](docs/MQTT.md)**.

#### 4c. Anything that can run `curl`: the check-in URL

For a cron job, a backup script, a router, a board that only speaks HTTP, or a machine behind a firewall. The device calls a
URL each time it is alive; if it stops calling, it goes OFFLINE.

1. **On the server:** `./fleet add backup-job --checkin` (add `--every 300` if it will check in every 5 minutes; the
   default is 60 seconds). The first time, it starts the check-in service and creates a key.
2. **It prints a command like this.** Run it on the device whenever it should report in:
   ```bash
   curl -fsS -m 10 "http://SERVER-ADDRESS:8081/checkin/backup-job?key=THE-KEY&every=60"
   ```
3. **For something periodic, put it in cron** (`crontab -e`; `./fleet add` prints the line for your interval):
   ```
   * * * * * curl -fsS -m 10 "http://SERVER-ADDRESS:8081/checkin/backup-job?key=THE-KEY&every=60" >/dev/null
   ```
   For a "the job finished" signal, put the `curl` at the **end** of your script: a failed run never reaches it, so the
   device goes OFFLINE and the alert fires.
4. **Add readings** with extra `&name=number` pairs, for example `&temperature_celsius=21.5&battery_volts=3.9`.

The device is OFFLINE after 3 times its `every` (at least 30 s). Details, errors and limits: **[docs/CHECKIN.md](docs/CHECKIN.md)**.

#### 4d. Something you cannot install anything on: watch it

A router, a NAS, a web service, a PLC's port, an ESP32 running ESPHome. The server checks it for you; nothing goes on
the device.

```bash
./fleet watch add router   ping    192.168.1.1                        # answers a ping
./fleet watch add nas      tcp     192.168.1.20:445                   # accepts a connection on that port
./fleet watch add pihole   http    http://192.168.1.2/admin/          # a web page answers
./fleet watch add printer  metrics http://192.168.1.50:7125/metrics   # serves Prometheus text; its numbers are stored too
./fleet watch list                                                    # what is watched, and how it is doing
```

It waits for the first answer and tells you if there is none, and why. Use IP addresses, not `.local` names (containers
usually cannot resolve those). Options and troubleshooting: **[docs/WATCH.md](docs/WATCH.md)**.

### 5. See it working

Open the dashboard (<http://SERVER-ADDRESS:3000>, or `localhost` on the server). Every device is one row: device ID,
state (ONLINE / OFFLINE), last seen, and CPU, memory, disk, temperature and uptime where the device has them. The graphs
below fill in over the next half minute. On the command line:

```bash
./fleet list              # every device: state, last seen, readings (--json for scripts)
./fleet check kitchen-pi  # one device in detail, including how it reports
./fleet status            # is the server healthy, and how many devices are online
```

**What OFFLINE means.** A device is OFFLINE when it has been quiet for longer than 3 times its reporting interval, and
never less than 30 seconds (the default agent reports every 5 s, so 30 s). The `DeviceOffline` alert fires about 30
seconds after that: roughly 70 seconds after a default agent stops. Try it: stop a device and watch its row turn red,
then open **Alerting, Alert rules, Device Fleet, DeviceOffline** in Grafana.

### 6. Get alerts on your phone (optional)

Out of the box an offline device only shows up if you open Grafana. To get a message instead, the easiest way is
**ntfy**: no account and no bot to set up.

1. Install the **ntfy** app on your phone (Android or iPhone, from the usual app store).
2. On the server run `./fleet notify set ntfy --random-topic`. It makes a long topic name nobody would guess (on the
   public ntfy.sh server, anyone who knows a topic name can read it) and tells you what it is, once.
3. In the app, press **+** and subscribe to that topic.

That is all. The other options need a bit more setup (a Telegram bot, or a webhook address from Discord or Slack):

```bash
./fleet notify set ntfy my-own-topic-name    # or your own ntfy server: ./fleet notify set ntfy https://ntfy.example.org/topic
./fleet notify set discord                   # asks for the webhook URL, with the input hidden
./fleet notify set slack https://hooks.slack.com/services/...
./fleet notify set telegram <bot-token> <chat-id>
./fleet notify set webhook https://your.service/endpoint   # anything that accepts a JSON POST
```

`set` hands the address to Grafana (which stores it encrypted; `fleet` never keeps it), sends a test message, and waits
until Grafana has switched over (up to about a minute: it applies alerting changes on a one-minute cycle). After that:

```bash
./fleet notify test      # send another test message
./fleet notify status    # where alerts go, and whether the last real delivery worked
./fleet notify off       # stop sending (alerts stay visible in Grafana)
```

You get one message per device when it goes offline and one when it is back. **What was tested:** real Grafana alerts
(a device going quiet, then back) delivered end to end to a webhook, and in ntfy's own message format to a stand-in ntfy
server; the Slack and Discord formats against a local listener only. **Not yet:** the real ntfy.sh and the phone app
(the test machine could not reach them) and Telegram. Email is not covered. See [docs/VALIDATION.md](docs/VALIDATION.md),
Runs E and J.

### 7. Day to day

| I want to... | Run |
|---|---|
| See everything | `./fleet list` or the dashboard |
| Retire a device for good | `./fleet remove kitchen-pi` (asks first, deletes its data; for a watched target use `./fleet watch remove`) |
| See or change the dashboard password | `./fleet password --show`, `./fleet password` (a random new one), `./fleet password --ask` (your own) |
| Stop the server, keep the data | `./fleet down` (asks first) |
| Stop it and delete all data | `./fleet down --wipe` (asks twice) |
| Start it again | `./fleet up` (it remembers MQTT, watching and check-in) |
| Update to a newer version | `git pull`, then `./fleet up` |
| Turn the MQTT input or check-in URL off | `./fleet up --no-mqtt`, `./fleet up --no-checkin` |

**After a power cut or a reboot** the server starts again by itself (the database, dashboard and collector restart with
Docker), as long as Docker starts at boot: `systemctl is-enabled docker` should say `enabled` (if not: `sudo systemctl
enable docker`). The agent service on a device starts at boot too. This was tested by restarting Docker, not by cutting
the power.

A retired device that is not removed stays on the dashboard as OFFLINE for a day, with its alert. Devices keep trying
while the server is down; after a short outage (about half a minute, the only length tested) they resumed by themselves.

### 8. Let an AI assistant or a script ask questions (optional)

The server can answer questions about itself, **read-only**: which devices are up, why one is offline, and the exact
steps to add a device of each kind (an assistant can use the last one to help you set things up). Two ways in, both
standard library only, so there is nothing extra to install:

```bash
python3 interface/http_api.py                     # then: curl http://127.0.0.1:8765/v1/list_devices
python3 interface/mcp_server.py --print-config    # prints the settings to paste into an AI assistant's MCP settings
```

Neither can change anything, neither shows a password, and the API listens on this machine only unless you give it a token.
The MCP server is written from scratch rather than using the SDK (no dependencies to be attacked through). Tested with the
official MCP client library; **not** yet with the Claude Desktop or Claude Code apps. Details, safety rules, and how to add
or remove a question: **[docs/INTERFACE.md](docs/INTERFACE.md)**.

### Not ready for real devices? Try it with two pretend ones

On the server itself, in the repository folder:

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r agent/requirements.txt
python agent/device_agent.py --device-id demo-pi-1        # terminal 1
python agent/device_agent.py --device-id demo-pi-2        # terminal 2 (each needs `. .venv/bin/activate` first)
```

Both rows appear within 15 to 20 seconds. Press **Ctrl+C** in the second terminal: `demo-pi-2` turns OFFLINE within
about 30 to 40 seconds and its alert fires about 70 seconds after you stopped it; start it again and it recovers
within about 20 s.

### Without the helper

`./fleet` only wraps these, so you can always run them directly:

```bash
docker compose up -d       # = ./fleet up, without waiting for readiness (Grafana then uses the password in .env, or `admin`)
docker compose down        # = ./fleet down, without asking (use `docker compose --profile '*' down` if MQTT, watching or check-in is on)
docker compose down -v     # = ./fleet down --wipe, without asking: ALSO DELETES stored data
```

### Check everything automatically

Each of these runs a whole scenario without a browser, exits non-zero on any failure, and uses test device IDs it
cleans up afterwards. Record results in [docs/VALIDATION.md](docs/VALIDATION.md).

| Script | Covers | Takes |
|---|---|---|
| `scripts/validate.sh` | the agent: two devices, one stopped, the alert (IDs `demo-pi-1`, `demo-pi-2`; `--help` for options) | about 3 min |
| `scripts/validate_mqtt.sh` | the MQTT input: access control, labels, a slow board, offline detection, the alert | about 4 min |
| `scripts/validate_watch.sh` | `./fleet watch`: ping, port, pages, a real exporter, a hostile page, stop and start, the alert | about 6 min |
| `scripts/validate_checkin.sh` | the check-in URL with the real container and `curl`: the key, bad input, a slow device, offline and back | about 3 min |
| `scripts/validate_interface.sh` | the read-only API and MCP server on the real stack: agreement with `./fleet list`, no leaked secrets, nothing changed, the network rules | about 3 min |

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
> A new install gets a random Grafana password; one made by an earlier version keeps `admin` / `admin` until you run
> `./fleet password`.

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

**I forgot the Grafana password.** First try `./fleet password --show`: it prints the one kept in `.env`. If you
changed it inside Grafana yourself, `./fleet` no longer knows it. Set a new one without touching your dashboard, alert
rule or data (replace `NEW-PASSWORD`), and tell `./fleet` about it:

```bash
docker compose exec grafana grafana cli admin reset-admin-password NEW-PASSWORD
```

Then put `GRAFANA_ADMIN_PASSWORD=NEW-PASSWORD` in the `.env` file. If your browser still rejects the new password, it may
be auto-filling an old saved one; try a private window.

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
`time() - tlast_over_time(device_uptime_seconds[1d])`, and it is **OFFLINE** when the age is above its
limit: 30 s (six missed pushes) for the default agent, or 3 times the interval the device declares in
`device_report_interval_seconds` when that is longer, so a device that reports slowly on purpose is not
flagged. The `DeviceOffline` rule uses the same query, evaluated every 10 s, with a 30 s pending
period, and produces one alert instance per device. Boards that speak MQTT join the same pipeline
through a small bridge (`bridge/mqtt_bridge.py`) that writes their readings to VictoriaMetrics; things
that cannot run anything are checked by a watcher (`watcher/watcher.py`) that writes the same series from
the server's side; and anything that can run `curl` reports through a small check-in service
(`watcher/checkin.py`). Whichever way a device reports, the offline rule above is the only one.

Vector (`vector.toml`) is not part of the default stack. It is parked behind the compose profile
`legacy-vector` because its demo configuration is broken; do not start it expecting it to work.

## Known limitations

- **No authentication or TLS** anywhere: agents send plaintext gRPC and anyone who can reach port 4317 can inject data. A new install gets a random Grafana password; one made by an earlier version keeps `admin` / `admin` until you run `./fleet password`.
- **No device registry.** A device that has never reported is invisible, and a device silent for more than a day drops off the dashboard. That is why `./fleet add` cannot "create" a device on the server: a device exists once it reports.
- **`./fleet` runs on the server only.** It prints the commands for a device but cannot install the agent there; on a device, `agent/install-service.sh` makes the agent start at boot (systemd only). `./fleet` and the installer were tested on x86_64 Linux, and the owner ran them on a Raspberry Pi 5 (see below).
- **Notifications are opt-in.** Until you run `./fleet notify set ...`, an alert is visible only inside Grafana (and its API). `fleet notify` covers ntfy, Telegram, Discord, Slack and webhooks, not email. Only the webhook path (and ntfy, against a stand-in server) was tested end to end; nothing has reached a real phone yet.
- **Offline detection is not instant.** With the default agent it takes about a minute from the device stopping to the alert firing; a device that reports every N seconds is OFFLINE after 3N (at least 30 s), and the alert follows 30 s later.
- **Watching is from the server's side only.** The watcher sees what its container sees, cannot send a login or token, and for a watched target "uptime" means time answering continuously, not time since boot. A target that has never answered is invisible, like any device. Tested against real containers and a real exporter (VictoriaMetrics' own `/metrics`) and ESPHome-style sample output; **no real ESPHome board, router or PLC was available**. See [docs/WATCH.md](docs/WATCH.md).
- **The check-in URL is plain HTTP with one shared key.** Anyone on your network who has the key can report as any device. A device can check in at most every 5 s and at slowest every 8 h (the dashboard looks back one day). Tested with `curl` against the container on x86, and the owner ran the printed command on a Raspberry Pi 5 and the device appeared; routers, `wget` and boards calling the URL themselves were not tried. See [docs/CHECKIN.md](docs/CHECKIN.md).
- **MQTT is plain and shared.** No TLS, one shared password for all boards, our `fleet/<device_id>/<metric>` topic scheme only (Tasmota, ESPHome, Zigbee2MQTT and Home Assistant topics are not mapped), and no Last Will handling: a board is OFFLINE by silence. Tested with simulated devices and a real broker, on x86 and on a Raspberry Pi 5 (the bridge built and ran on arm64); **no real ESP32, Pico or Arduino was available**. See [docs/MQTT.md](docs/MQTT.md).
- **The question interface (`interface/`) is read-only, local by default, and plain HTTP.** It listens on this machine only unless `FLEET_API_TOKEN` is set, and has no TLS. It was tried with the official MCP client library and `curl`, not with real assistant apps, and not from another machine. See [docs/INTERFACE.md](docs/INTERFACE.md).
- **Temperature** is reported only where `/sys/class/thermal` provides it.
- **Single node, demo scale.** One VictoriaMetrics, one Collector, one Grafana, no redundancy or retention tuning.
- **Verified on x86_64 Linux and on one Raspberry Pi 5** (aarch64, Debian 13 "trixie"), where the stack and both demo agents ran on the same Pi and the dashboard was viewed from a laptop. Later, run by the Pi's owner on the same Pi: the agent installed as a service, a check-in URL device, a ping-watched router, and the MQTT broker and bridge with a simulated board. **Not yet verified:** a separate device pushing to the server over the network, 32-bit Raspberry Pi OS, Pi Zero-class hardware, and real ESP32-class boards (the agent is Linux-only; they use the MQTT input, tested only with simulated devices). See [docs/VALIDATION.md](docs/VALIDATION.md).
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
