# Integration contract: multi-device monitoring

Single source of truth for the device agent, dashboard/alert, and docs.
Owned by the main agent. If something here is wrong or insufficient, report it
instead of working around it.

## Data path (unchanged existing infrastructure)

```
device_agent.py --OTLP gRPC--> otel-collector :4317 --remote write--> VictoriaMetrics :8428 <-- Grafana :3000
```

No changes to `otel-collector.yaml`. Resource attributes reach VM as
`job` (from `service.name`) and `instance` (from `service.instance.id`).
Data-point attributes become labels.

## Device identity

- `device_id`: unique, user-chosen, stable across restarts and reboots.
  Pattern `^[a-z0-9][a-z0-9._-]{0,62}$`. Default: lowercased hostname.
  Config: `--device-id` flag or `DEVICE_ID` env var (flag wins).
- `hostname`: informational only (`socket.gethostname()`). Identity is `device_id`.
- Demo IDs: `demo-pi-1`, `demo-pi-2`.

## Resource attributes (set by the agent)

| Attribute | Value | Stored in VM as |
|---|---|---|
| `service.name` | `device-agent` | `job="device-agent"` |
| `service.instance.id` | the `device_id` | `instance="<device_id>"` |

## Metrics (names are exactly as stored in VM)

All are gauges. **Set no unit** on the instrument, so the Collector does not
append a suffix. Units are part of the name. Every data point carries the labels
`device_id` and `hostname`.

| Metric | Meaning | Range |
|---|---|---|
| `device_cpu_utilization_percent` | Busy CPU share across all cores since the previous sample | 0-100 |
| `device_memory_utilization_percent` | `(MemTotal - MemAvailable) / MemTotal` | 0-100 |
| `device_disk_utilization_percent` | `used / (used + avail)` of `--disk-path` (default `/`), as `df` reports it | 0-100 |
| `device_uptime_seconds` | Seconds since boot (`/proc/uptime`) | >= 0 |
| `device_temperature_celsius` | Hottest readable `/sys/class/thermal/thermal_zone*/temp` (millidegrees / 1000); values outside -40..150 are ignored | |
| `device_report_interval_seconds` | **Optional.** How often this device promises to report. Sets how long it may stay quiet before it counts as OFFLINE (see below). The Linux agent sends its `--interval`. | > 0 |

Rules:

- **Unavailable sensor means no data point at all.** Never emit 0 or NaN as a
  placeholder. Today only temperature can be absent. In the fleet table a device
  without a reading shows "N/A" once at least one device reports a temperature;
  if none does, the temperature column is not shown.
- **Liveness metric is `device_uptime_seconds`.** It is always emitted, so it
  defines "last seen". Every other metric may be missing, including
  `device_report_interval_seconds`. A device that reports an interval but no
  uptime is invisible.
- Use **observable gauges with callbacks** so a fresh sample is produced on
  every export, and the series keeps moving while the device is alive.
- Push interval defaults to 5 s (`--interval` / `PUSH_INTERVAL`).

## Agent configuration

| Setting | Flag | Env var | Default |
|---|---|---|---|
| Device ID | `--device-id` | `DEVICE_ID` | lowercased hostname |
| OTLP endpoint (`host:port`, plaintext gRPC) | `--endpoint` | `OTLP_ENDPOINT` | `localhost:4317` |
| Push interval, seconds | `--interval` | `PUSH_INTERVAL` | `5` |
| Disk path | `--disk-path` | `DISK_PATH` | `/` |
| Print one JSON snapshot and exit, send nothing | `--dry-run` | | off |

Flag beats env var beats default. A leading `http://` on the endpoint is tolerated and stripped.

## Online / offline semantics

- **Last seen** (epoch seconds): `tlast_over_time(device_uptime_seconds[1d])`
- **Age** (seconds): `time() - tlast_over_time(device_uptime_seconds[1d])`
- **Limit** (seconds of silence allowed): `max(30, 3 x device_report_interval_seconds)`.
  A device that declares no interval is held to 30 s, which is what a default 5 s
  agent gets anyway (six missed pushes). One rule, used by the dashboard, the alert
  and `./fleet`:
  ```
  (clamp_min(3 * max by (device_id) (last_over_time(device_report_interval_seconds[1d])), 30) or (0 * max by (device_id) (time() - tlast_over_time(device_uptime_seconds[1d])) + 30))
  ```
- **OFFLINE** when age > limit. Otherwise ONLINE. The declared interval is read from
  the last day of data, so a device that stops reporting keeps the limit it last declared.
- `tlast_over_time` is MetricsQL (VictoriaMetrics). The Grafana datasource is
  of type `prometheus` pointing at VM, which accepts it. Do not use plain
  `timestamp()`. It returned nothing in testing.
- VM runs with `-search.latencyOffset=5s` (set in `docker-compose.yml`). The default
  of 30 s hides the newest data and shifts `time()` back by the same amount.
  Do not rely on VM's `time()` matching the wall clock to within 5 s.
- Devices that have never reported are invisible. There is no device registry.
- A device silent for more than 1 day drops off the dashboard (query window).

## MQTT reporter (optional; `docs/MQTT.md` is the user guide)

Boards that cannot run the agent publish MQTT messages; `bridge/mqtt_bridge.py` (compose services `mosquitto` and
`mqtt-bridge`, profiles `mqtt` and `mqtt-bridge`, settings in `.env`) turns them into the same metrics. The data path
for these devices is `board -> broker -> mqtt-bridge -> VictoriaMetrics /api/v1/import/prometheus`; the OTel Collector
is not involved.

| MQTT topic | Stored as |
|---|---|
| `fleet/<device_id>/<metric>`, payload a plain number | `device_<metric>{device_id="<device_id>",job="mqtt-bridge"}` |
| `fleet/<device_id>/report_interval_seconds` | held by the bridge and written as `device_report_interval_seconds` next to every `uptime_seconds` sample |

- **Names.** `device_id` follows the rule above. `<metric>` matches `^[a-z][a-z0-9_]{0,62}$`. Anything else is dropped.
- **Labels.** Only `device_id` and `job="mqtt-bridge"`. There is no `hostname` (the table column shows `-`) and no
  `instance`. `fleet remove` deletes by `device_id`, so it covers these series too.
- **Liveness is still `device_uptime_seconds`**, so a board that never publishes `uptime_seconds` is invisible (the
  bridge warns about it after 30 s). The value itself is not interpreted.
- **Timestamps are the bridge's arrival time**, not the board's. If VictoriaMetrics is unreachable the bridge keeps up
  to 20,000 lines and delivers them with their original arrival times.
- **Retained messages are never readings** (the broker replays them on every reconnect, so they would make a dead
  device look alive). The one exception is `report_interval_seconds`, which is configuration: valid range 0 to 86400.
- **Limits.** 500 devices, 64 metrics per device.
- **Broker (bundled).** User `fleet`, password generated into `.env` (mode 600, gitignored), access list `fleet/#`
  only, plain MQTT on 1883. Last Will / status topics are not used.

## Watcher (optional; `docs/WATCH.md` is the user guide)

Things that cannot run the agent (a router, a NAS, a service, a board that serves Prometheus text) are checked by
`watcher/watcher.py` (compose service `watcher`, profile `watch`, target list in `watch/targets.json` managed by
`./fleet watch`). Kinds: `ping HOST`, `tcp HOST:PORT`, `http URL`, `metrics URL`. Data path:
`watcher -> VictoriaMetrics /api/v1/import/prometheus`; the OTel Collector is not involved.

Written on **every** check, stamped with the time the check finished, labels `device_id`, `job="watcher"`, `kind`:

| Series | Value |
|---|---|
| `device_probe_success` | 1 if the check succeeded, else 0 |
| `device_probe_duration_seconds` | how long the check took (for ping: the reply time) |
| `device_report_interval_seconds` | the target's check interval (so the limit above applies: `max(30 s, 3 x interval)`) |
| `device_http_status_code` | the HTTP status, for `http` and `metrics` targets that got an answer |

Written **only when the check succeeded**:

| Series | Value |
|---|---|
| `device_uptime_seconds` | **observed uptime**: seconds the target has answered *continuously*. Zero on the first answer after a gap. This is not time since boot; it is the liveness signal, so a target that stops answering goes quiet and becomes OFFLINE like any other device. |
| the scraped series (`metrics` only) | the target's own numbers under their own names, with the labels `device_id`, `job`, `kind` added. A scraped label of those names is kept as `exported_<name>`. |
| `device_scrape_series` | how many scraped series were kept |

- A target that has never answered is invisible (the same rule as for every device). `./fleet watch list` shows it as
  `waiting`, and `./fleet watch add` reports why from the watcher's log.
- **Reserved:** scraped names starting with `device_` are dropped, so a scraped page cannot overwrite the series above.
  NaN and infinite values are dropped. At most `max_series` (default 2000, up to 10000, the first ones the page lists) and 5 MB are taken from one answer. A page that is not
  Prometheus text (a web page) is a failed check, not a healthy target with no data.
- `http` succeeds on 2xx and 3xx (after redirects), or on exactly `expect_status` when one is set. `metrics` needs 200
  and at least one usable series. Certificates are verified unless the target says `insecure`.
- Limits: 100 targets, interval 5 to 3600 s, timeout up to the interval. No login, token or header can be sent.
- There is no `hostname` label (the table column shows `-`).

## Grafana (provisioned from files, nothing configured by hand)

| Item | Value |
|---|---|
| Datasource | name `VictoriaMetrics`, uid `victoriametrics`, type `prometheus`, url `http://victoriametrics:8428`, default |
| Dashboard | uid `device-fleet`, title `Device Fleet`, file `grafana/dashboards/device-fleet.json` (the compose home-dashboard env var points at this filename) |
| Dashboard columns | device_id, hostname, state (ONLINE/OFFLINE, colour coded), last seen, CPU %, memory %, disk %, temperature, uptime |
| Alert rule | uid `device-offline`, title `DeviceOffline`, folder `Device Fleet`, group `device-health` |
| Alert behaviour | one alert instance per `device_id`; condition age > limit (above); pending `for: 30s`; evaluated every `10s`; labels `severity=critical` (`device_id` comes from the query labels); `noDataState: OK` (a device with no series is not alerting); `execErrState: Error` (if VictoriaMetrics is unreachable Grafana raises a separate DatasourceError alert rather than marking every device DeviceOffline) |
| Notifications | No contact point is configured. State is visible in Grafana Alerting and through its API. This is a documented limitation. |

Mounts (already in `docker-compose.yml`):
`./grafana/provisioning` to `/etc/grafana/provisioning` (read-only) and
`./grafana/dashboards` to `/var/lib/grafana/dashboards` (read-only).
Grafana login is the default `admin` / `admin`.

## File ownership (no overlaps)

| Owner | Files |
|---|---|
| Main agent | `docker-compose.yml`, `otel-collector.yaml`, `vector.toml`, `.gitignore`, `docs/CONTRACT.md`, all git commits |
| Role A | `agent/**`, `tests/test_device_agent.py` |
| Role B | `grafana/**` |
| Role C | `README.md` (the quickstart lives at its top, with no second copy), `docs/VALIDATION.md`, `scripts/**` |

Not to be touched by anyone: `clinic_simulator.py`, `send_metrics.py`, `p1.png`, `p2.png`.

## Shared-environment rules for parallel work

- The stack is running now (`docker compose ps`). Do not run `docker compose down`
  or `down -v`, and do not delete VM data. Main agent does the clean reset.
- Test devices must use IDs starting with `test-`, so they are easy to tell apart from demo data.
- Role B may restart Grafana (`docker compose restart grafana`) to reload
  provisioning, and may call the Grafana HTTP API at `localhost:3000`.
- Python: use a venv, never `--break-system-packages`. A ready one with the OTel
  packages is at `$SCRATCH/venv`. Documented user commands use `.venv/`.
- Do not `git add` or commit. Report to the main agent when done.
- Do not add dependencies beyond `opentelemetry-sdk` and
  `opentelemetry-exporter-otlp-proto-grpc` without asking.
