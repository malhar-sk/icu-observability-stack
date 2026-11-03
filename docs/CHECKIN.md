# Check-in URL: anything that can run `curl`

For a cron job, a backup script, a router, a board that only speaks HTTP, or a machine behind a firewall. The device
calls a URL each time it is alive. If it stops calling, it goes OFFLINE and the `DeviceOffline` alert fires, exactly
like a device running the agent.

```
 a cron job, script, router ... --- curl "http://SERVER:8081/checkin/<id>?key=KEY&every=60" --->  server --> dashboard, alert
```

Demonstration-grade, like the rest of this repo: plain HTTP and one shared key. Keep it on a network you trust. Not for
medical or safety use.

## Set it up

```bash
./fleet add backup-job --checkin               # every 60 s by default; --every 300 for every 5 minutes
```

The first time, this starts the check-in service (the stock `python:3.13-alpine` image, about 70 MB on disk, nothing to
build), creates a key in `.env` (private to you, not committed) and prints what to run on the device. Then it waits for
the first check-in:

```bash
curl -fsS -m 10 "http://SERVER-ADDRESS:8081/checkin/backup-job?key=THE-KEY&every=60"
```

- `-f` makes curl fail on an error answer, `-sS` keeps it quiet but still shows errors, `-m 10` gives up after 10 s.
- **Quote the address**: it contains `&`.
- **`every=`** is how often the device will check in, in seconds (5 to 28800). It is OFFLINE after **3 times that**, and never
  before 30 s. Say it honestly: a job that runs hourly with `every=60` would be OFFLINE almost all the time.
- The key is shared by every device. `./fleet add` prints it each time; it is also in `.env` as `CHECKIN_KEY`.

## Typical uses

**A cron job** (`crontab -e`; `./fleet add` prints the line for your interval):

```
* * * * * curl -fsS -m 10 "http://SERVER-ADDRESS:8081/checkin/backup-job?key=THE-KEY&every=60" >/dev/null
```

**"The job finished" (a backup, a sync).** Put the `curl` as the last line of the script, after the work succeeded. A
failed run never reaches it, so the device goes OFFLINE and you get the alert. Use `every=` a little over how often
the job runs.

```bash
#!/bin/bash
set -e
rsync -a /data/ nas:/backup/ && curl -fsS -m 10 "http://SERVER-ADDRESS:8081/checkin/nightly-backup?key=THE-KEY&every=28800"
```

**With readings.** Add any number of `&name=number` pairs; each is stored as `device_<name>` (look at it in Grafana
Explore):

```bash
curl -fsS -m 10 "http://SERVER-ADDRESS:8081/checkin/garage?key=THE-KEY&every=60&temperature_celsius=21.5&battery_volts=3.9"
```

Names are lowercase letters, digits and `_`, starting with a letter; values are plain numbers. `uptime_seconds=<n>` lets the
device send its own uptime; otherwise the server counts how long it has checked in continuously.

## What you get

- The device appears on the dashboard the first time it checks in, with state, last seen and uptime (a device that checks in
  has no CPU or memory, so those show N/A), and `./fleet check <id>` says "via check-in URL".
- **Uptime is observed:** the seconds it has checked in without a gap longer than its limit. It starts over after one.
- The slowest `every` is 28800 (8 hours): the dashboard looks back one day, and the limit is three times `every`. A daily
  job cannot be shown this way; check in from it more often, or from a wrapper that runs every few hours.

## When something does not work

Run the `curl` by hand without `-s` and read the answer:

| The answer | It means |
|---|---|
| `wrong or missing key` (401) | the key does not match `CHECKIN_KEY` in `.env` (or the address was not quoted and `&` cut it off) |
| `the device id must be ...` (400) | the id in the address is not lowercase letters, digits, `.`, `_`, `-` |
| `every must be a number of seconds from 5 to 28800` | a bad `every` |
| `<name> must be a plain number such as 21.5` | a reading that is not a number (text, `nan`, empty) |
| `use /checkin/<device_id>?key=...` (404) | the path is wrong |
| `Connection refused` | the service is not running: `./fleet add <id> --checkin` starts it, or check `docker compose logs checkin` |
| `port 8081 is already in use` (from `./fleet add`) | put `CHECKIN_PORT=8090` (any free port) in `.env` and run it again |

The service's log (`docker compose logs checkin`) says when a check-in was refused and why, once per device and reason. It
never contains the key.

## Good to know

- **Keep the key private.** Anyone on your network who has it can report as any device. To change it, delete the
  `CHECKIN_KEY=` line from `.env`, run `./fleet add <id> --checkin` (it makes a new one), and update every device.
- Turn the service off with `./fleet up --no-checkin`. The key stays in `.env`, so the commands you already gave to devices
  work again if you turn it back on.
- Limits: 500 devices, 64 readings per request, addresses up to 2000 characters. IPv6 was not tried.
- **Tested with `curl` only:** against the container on x86, and the printed command on a Raspberry Pi 5, where the device
  appeared. Routers, busybox `wget` and boards that call the URL themselves were not tried.
