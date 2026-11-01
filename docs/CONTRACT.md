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

Rules:

- **Unavailable sensor means no data point at all.** Never emit 0 or NaN as a
  placeholder. Today only temperature can be absent. In the fleet table a device
  without a reading shows "N/A" once at least one device reports a temperature;
  if none does, the temperature column is not shown.
- **Liveness metric is `device_uptime_seconds`.** It is always emitted, so it
  defines "last seen". Every other metric may be missing.
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
- **OFFLINE** when age > `30` s (six missed 5 s pushes). Otherwise ONLINE.
- `tlast_over_time` is MetricsQL (VictoriaMetrics). The Grafana datasource is
  of type `prometheus` pointing at VM, which accepts it. Do not use plain
  `timestamp()`. It returned nothing in testing.
- VM runs with `-search.latencyOffset=5s` (set in `docker-compose.yml`). The default
  of 30 s hides the newest data and shifts `time()` back by the same amount.
  Do not rely on VM's `time()` matching the wall clock to within 5 s.
- Devices that have never reported are invisible. There is no device registry.
- A device silent for more than 1 day drops off the dashboard (query window).

## Grafana (provisioned from files, nothing configured by hand)

| Item | Value |
|---|---|
| Datasource | name `VictoriaMetrics`, uid `victoriametrics`, type `prometheus`, url `http://victoriametrics:8428`, default |
| Dashboard | uid `device-fleet`, title `Device Fleet`, file `grafana/dashboards/device-fleet.json` (the compose home-dashboard env var points at this filename) |
| Dashboard columns | device_id, hostname, state (ONLINE/OFFLINE, colour coded), last seen, CPU %, memory %, disk %, temperature, uptime |
| Alert rule | uid `device-offline`, title `DeviceOffline`, folder `Device Fleet`, group `device-health` |
| Alert behaviour | one alert instance per `device_id`; condition age > 30 s; pending `for: 30s`; evaluated every `10s`; labels `severity=critical` (`device_id` comes from the query labels); `noDataState: OK` (a device with no series is not alerting); `execErrState: Error` (if VictoriaMetrics is unreachable Grafana raises a separate DatasourceError alert rather than marking every device DeviceOffline) |
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
