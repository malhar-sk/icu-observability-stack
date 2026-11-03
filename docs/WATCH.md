# Watching things you cannot install anything on

Some things cannot run the Linux agent, or you would rather not touch them: a router, a NAS, a service, a PLC's port, a
Pi you only need to know is alive, an ESP32 running ESPHome. `./fleet watch` checks them from the server instead. They
show up in the same **Device Fleet** dashboard with the same ONLINE/OFFLINE state and the same `DeviceOffline` alert.

```
 the server's watcher --ping / TCP / HTTP / scrape--> your router, NAS, service, ESPHome board ...
        |
        v  the same device_* metrics
 VictoriaMetrics <---- Grafana (dashboard, alert)
```

Demonstration-grade, like the rest of this repo. Not for medical or safety use.

## Use it

```bash
./fleet watch add router   ping    192.168.1.1                       # answers a ping
./fleet watch add nas      tcp     192.168.1.20:445                  # accepts a connection (a port, a PLC, SSH)
./fleet watch add pihole   http    http://192.168.1.2/admin/         # a web page answers
./fleet watch add printer  metrics http://192.168.1.50:7125/metrics  # serves Prometheus text; its numbers are stored too
./fleet watch list                                                   # what is watched, and how it is doing
./fleet watch remove router                                          # stop, and delete its stored data (asks first)
```

`add` starts the watcher the first time (it uses the stock `python:3.13-alpine` image, about 70 MB on disk, nothing to
build), checks the target, and tells you if it does not answer and why. It stays on the list either way: a device that
is switched off today still appears the moment it first answers. The list lives in `watch/targets.json` (not
committed); the watcher notices changes by itself, so adding or removing never restarts anything.

| Option | For | Meaning |
|---|---|---|
| `--interval SECONDS` | all | how often to check (default 15, 5 to 3600) |
| `--timeout SECONDS` | all | how long to wait for an answer (default 5, never more than the interval) |
| `--expect-status CODE` | `http` | the status that counts as healthy (default: any 2xx or 3xx, after redirects) |
| `--insecure` | `https` | accept a self-signed certificate (certificates are verified otherwise) |
| `--max-series N` | `metrics` | keep at most this many series from the page (default 2000, up to 10000) |

## What you get

- **State.** A target is OFFLINE after `max(30 s, 3 x its interval)` without an answer: 45 s at the default interval. One
  lost ping does not flap it, three in a row do. The `DeviceOffline` alert fires about 30 s after that.
- **Uptime is observed.** For a watched target the "uptime" column is how long it has answered *continuously*, not time
  since boot. It starts over after every gap.
- **Everything the check saw**, in Grafana Explore: `device_probe_success`, `device_probe_duration_seconds` (for ping, the
  reply time), `device_http_status_code`, all with the labels `device_id`, `job="watcher"` and `kind`.
- **Scraped numbers** (kind `metrics`): every series the page serves is stored under its own name with `device_id` added,
  for example `esphome_sensor_value{device_id="greenhouse",name="Temperature"}`. The dashboard table does not draw them
  (its columns are the standard `device_*` readings); look at them in Explore or build a panel.
  Names starting with `device_` are dropped (so a page cannot overwrite our series), NaN and infinity are dropped, and
  at most 2000 series (the first ones the page lists; `--max-series` changes it) and 5 MB are taken from one answer. The
  watcher says so in its log, once. Every series is stored on every check, so raise the limit (or the interval) only if you need to.
- A page that is not Prometheus text (a web page) is a **failed** check, not a healthy target with no data.

## ESP32 and other boards

- **ESPHome**: add `web_server:` and `prometheus:` to the board's YAML (ESPHome's own docs describe them), flash, then
  `./fleet watch add greenhouse metrics http://<its-ip>/metrics`. Tested against ESPHome-style output, not against a
  real ESPHome board.
- **Your own firmware on an ESP32, Pico W or similar**: use [MQTT](MQTT.md) (the board reports; it also covers boards that
  sleep between readings).
- **A Raspberry Pi**: install the agent for CPU, memory, disk and temperature. If you only need to know it is alive, ping it.
  A Pi Zero 2 W or a Pi 5 can be watched either way.

## Good to know

- **Use IP addresses (or real DNS names), not `.local` names.** The watcher runs in a Docker container, which usually
  cannot resolve mDNS names like `raspberrypi.local`. `./fleet watch add` warns you about this. Reserve the target's
  address in your router so it does not change.
- The watcher sees the network the container sees. A target reachable only through something on the host (a VPN, say)
  may not be reachable from it.
- **No logins.** No password, token or header can be sent, and there are no client certificates. Point it at pages that
  answer without one.
- The watcher makes requests to whatever you list, from the server. Do not point it at things you do not own.
- Limits: 100 targets. IPv6 is accepted but was not tested.
- If a target flaps because the network is lossy, raise `--interval` (the limit is three intervals) rather than the timeout.

## When something does not work

```bash
./fleet watch list                 # 'waiting' = it has never answered
docker compose logs watcher        # says UP and DOWN once per change, with the reason
```

| The log says | It means |
|---|---|
| `cannot resolve <name>` | the name does not exist (or is a `.local` name): use the IP address |
| `no reply` / `timed out` | nothing answered in time (off, firewalled, wrong address) |
| `Connection refused` | the host is up but nothing listens on that port |
| `HTTP 404` (or 500...) | the page answered with an error status; use `--expect-status` if that is normal |
| `the certificate was refused ...` | a self-signed certificate: add `--insecure` |
| `HTTP 200, but it is not Prometheus text (a web page?)` | the address is not a metrics page |
| `metric names starting with device_ are reserved and were dropped` | the page serves names that collide with ours |
| `more than 2000 series: the rest were dropped` | a very large exporter; the first ones are kept. Raise it with `--max-series` if you need the rest |
