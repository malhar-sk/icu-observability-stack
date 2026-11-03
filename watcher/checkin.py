#!/usr/bin/env python3
"""Check-in service: anything that can run `curl` can report in as a device, so a cron job, a backup script, a router,
a board that only speaks HTTP, or a machine behind a firewall shows up in the Device Fleet dashboard with the same
ONLINE/OFFLINE state and the same DeviceOffline alert.

    curl -fsS "http://SERVER:8081/checkin/backup-job?key=KEY&every=3600"
    curl -fsS "http://SERVER:8081/checkin/garage?key=KEY&every=60&temperature_celsius=21.5&humidity_percent=48"

    /checkin/<device_id>   the device's name (lowercase letters, digits, '.', '_' and '-')
    key=KEY                the shared key from .env (required; it is never written to the log)
    every=SECONDS          how often it will check in (5 to 28800); it is OFFLINE after 3x this, at least 30 s
    <name>=<number>        any reading, stored as device_<name> (temperature_celsius=21.5, battery_volts=3.9, ...)

Each check-in writes device_uptime_seconds (seconds it has checked in continuously: it starts over after a gap longer
than its limit; send uptime_seconds=<n> yourself to override), device_report_interval_seconds (when every= was given)
and your readings, with the labels device_id and job="checkin", stamped on arrival. See docs/CHECKIN.md and
docs/CONTRACT.md. Standard library only; plain HTTP, so keep it on a network you trust.
"""
import argparse
import hmac
import http.server
import importlib.util
import logging
import math
import os
import re
import signal
import sys
import threading
import time
import urllib.parse

log = logging.getLogger("checkin")

# The delivery code is shared with the watcher (same folder), loaded by path so this works wherever it is run from.
_spec = importlib.util.spec_from_file_location("fleet_watcher_core", os.path.join(os.path.dirname(os.path.abspath(__file__)), "watcher.py"))
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)

JOB = "checkin"
METRIC_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
RESERVED_PARAMS = ("key", "every")
MIN_EVERY, MAX_EVERY = 5, 28800      # 28800 s = 8 h: the offline limit (3x) then stays inside the dashboard's one-day lookback
MAX_DEVICES = 500
MAX_PARAMS = 64
MAX_URL = 2000
MAX_BODY = 4096


class CheckIns(core.Pusher):
    """Turns one check-in into metric lines. Thread safe."""

    def __init__(self, vm_url, key, clock=time.time):
        super().__init__(vm_url)
        self.key = key
        self.clock = clock
        self.devices = {}   # device_id -> {"last": time of the last check-in, "since": start of the streak, "every": declared or None}
        self.stats.update(accepted=0, refused=0)

    def handle(self, device_id, params, key):
        """(HTTP status, text). `params` is {name: [values]} from the query string, without the key."""
        if not self.key or not hmac.compare_digest(str(key).encode(), self.key.encode()):
            self.stats["refused"] += 1
            return 401, "wrong or missing key"
        if not core.DEVICE_ID_RE.fullmatch(device_id):
            return 400, "the device id must be lowercase letters, digits, '.', '_' and '-' (up to 63, starting with a letter or digit)"
        if len(params) > MAX_PARAMS:
            return 400, f"too many readings (at most {MAX_PARAMS})"
        every, readings = None, {}
        for name, values in params.items():
            if len(values) != 1:
                return 400, f"{name} was given more than once"
            value = values[0]
            if name == "every":
                if not core.NUMBER_RE.fullmatch(value) or not math.isfinite(float(value)) or not MIN_EVERY <= float(value) <= MAX_EVERY:
                    return 400, f"every must be a number of seconds from {MIN_EVERY} to {MAX_EVERY}"
                every = float(value)
            elif not METRIC_RE.fullmatch(name):
                return 400, f"'{name[:40]}' is not a reading name (lowercase letters, digits and '_', starting with a letter)"
            elif name == "report_interval_seconds":
                return 400, "use every=SECONDS to say how often you check in"
            elif not core.NUMBER_RE.fullmatch(value) or not math.isfinite(float(value)):
                return 400, f"{name} must be a plain number such as 21.5"
            else:
                readings[name] = float(value)
        with self.lock:
            now = self.clock()
            state = self.devices.get(device_id)
            if state is None:
                if len(self.devices) >= MAX_DEVICES:
                    return 503, f"too many devices (at most {MAX_DEVICES})"
                state = self.devices[device_id] = {"last": None, "since": now, "every": None}
            if every is not None:
                state["every"] = every
            limit = max(30.0, 3 * state["every"]) if state["every"] else 30.0
            if state["last"] is None or now - state["last"] > limit:
                state["since"] = now  # it was away for longer than it is allowed to be: a new streak
            state["last"] = now
            stamp = int(now * 1000)
            base = {"device_id": device_id, "job": JOB}
            uptime = readings.pop("uptime_seconds", None)
            lines = [core.format_line("device_uptime_seconds", base, round(now - state["since"], 3) if uptime is None else uptime, stamp)]
            if state["every"]:
                lines.append(core.format_line("device_report_interval_seconds", base, state["every"], stamp))
            lines += [core.format_line(f"device_{name}", base, value, stamp) for name, value in readings.items()]
            self._extend_locked(lines)
            self.stats["accepted"] += 1
        return 200, "ok"


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "fleet-checkin"
    protocol_version = "HTTP/1.0"
    timeout = 10  # a client that stalls must not hold a thread forever

    def log_message(self, *args):
        pass  # the default logs the whole request line, which holds the key

    def _reply(self, status, text):
        body = (text + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _serve(self):
        if len(self.path) > MAX_URL:
            return self._reply(414, "address too long")
        length = self.headers.get("Content-Length", "")
        if length.isdigit() and 0 < int(length) <= MAX_BODY:
            self.rfile.read(int(length))  # the body is not used, but reading it keeps the connection tidy
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/health":
            return self._reply(200, "ok")
        m = re.fullmatch(r"/checkin/([^/]+)", url.path)
        if not m:
            return self._reply(404, "use /checkin/<device_id>?key=KEY&every=SECONDS (see docs/CHECKIN.md)")
        try:
            params = urllib.parse.parse_qs(url.query, keep_blank_values=True, max_num_fields=MAX_PARAMS + 4)
        except ValueError:
            return self._reply(400, "too many parameters")
        key = params.pop("key", [""])[-1]
        device_id = urllib.parse.unquote(m.group(1))
        status, text = self.server.checkins.handle(device_id, params, key)
        if status != 200:
            self.server.checkins.warn_once((status, device_id), "%s: refused (%d): %s", device_id[:64], status, text)
        self._reply(status, text)

    do_GET = do_POST = do_HEAD = _serve


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, checkins):
        super().__init__(address, Handler)
        self.checkins = checkins


def parse_config(argv=None, env=None):
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(description="Let anything that can run curl report in as a device.")
    ap.add_argument("--port", type=int, default=8081, help="port to listen on (default 8081; docker-compose publishes it)")
    ap.add_argument("--vm-url", help="VictoriaMetrics address (env VM_URL, default http://victoriametrics:8428)")
    a = ap.parse_args(argv)
    if not 0 < a.port < 65536:
        ap.error("invalid port")
    key = env.get("CHECKIN_KEY") or ""
    if not key:
        ap.error("CHECKIN_KEY is empty: start this with ./fleet add <id> --checkin, which creates the key")
    return argparse.Namespace(port=a.port, key=key, vm_url=a.vm_url or env.get("VM_URL") or "http://victoriametrics:8428")


def run(cfg):
    checkins = CheckIns(cfg.vm_url, cfg.key)
    server = Server(("0.0.0.0", cfg.port), checkins)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("listening on port %d; readings go to %s", cfg.port, cfg.vm_url)
    last_stats = time.time()
    while not stop.wait(1.0):
        checkins.flush_logged()
        if time.time() - last_stats >= 3600:
            last_stats = time.time()
            log.info("last hour: %s", ", ".join(f"{k}={v}" for k, v in checkins.stats.items()))
    server.shutdown()
    checkins.flush_logged()
    log.info("stopped")


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(parse_config(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
