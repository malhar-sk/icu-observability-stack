#!/usr/bin/env python3
"""Minimal device health agent: pushes ONE device's telemetry to an OTLP/gRPC collector.

Names, labels and config follow docs/CONTRACT.md. Linux only; stdlib + OpenTelemetry SDK.
Usage: python agent/device_agent.py [--device-id ID] [--endpoint HOST:PORT] [--dry-run]
"""
import argparse
import glob
import json
import logging
import math
import os
import re
import signal
import socket
import sys
import threading
import time

log = logging.getLogger("device-agent")

DEVICE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
THERMAL_GLOB = "/sys/class/thermal/thermal_zone*/temp"
TEMP_MIN_C, TEMP_MAX_C = -40.0, 150.0
METRICS = ("device_cpu_utilization_percent", "device_memory_utilization_percent",
           "device_disk_utilization_percent", "device_uptime_seconds", "device_temperature_celsius")
# How often this device promises to report. The dashboard and the alert use it to decide when "quiet" means
# OFFLINE (3 x this, never less than 30 s), so a device that reports slowly on purpose is not flagged.
INTERVAL_METRIC = "device_report_interval_seconds"


# Pure parsers: take file contents or stat results, so they are easy to unit test.
def clamp_percent(x):
    return max(0.0, min(100.0, x))


def parse_cpu_times(stat_text):
    """(busy, total) jiffies from the aggregate 'cpu' line of /proc/stat."""
    for line in stat_text.splitlines():
        parts = line.split()
        if parts and parts[0] == "cpu":
            v = [int(x) for x in parts[1:9]]  # user nice system idle iowait irq softirq steal
            if len(v) < 4:
                raise ValueError("short cpu line in /proc/stat")
            return sum(v) - sum(v[3:5]), sum(v)  # idle and iowait both count as idle
    raise ValueError("no aggregate cpu line in /proc/stat")


def cpu_percent(prev, cur):
    """Busy share 0-100 between two (busy, total) samples; None if no time elapsed."""
    elapsed = cur[1] - prev[1]
    return clamp_percent(100.0 * (cur[0] - prev[0]) / elapsed) if elapsed > 0 else None


def parse_meminfo_percent(text):
    """(MemTotal - MemAvailable) / MemTotal as 0-100."""
    kb = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            kb[key] = int(parts[0])
    total = kb.get("MemTotal")
    if not total:
        raise ValueError("MemTotal missing from /proc/meminfo")
    avail = kb.get("MemAvailable")
    if avail is None:  # kernels before 3.14
        avail = kb.get("MemFree", 0) + kb.get("Buffers", 0) + kb.get("Cached", 0)
    return clamp_percent(100.0 * (total - avail) / total)


def parse_uptime(text):
    """Seconds since boot from /proc/uptime."""
    return max(0.0, float(text.split()[0]))


def disk_percent(st):
    """used / (used + avail) like df, from an os.statvfs result."""
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    avail = st.f_bavail * st.f_frsize
    if used + avail <= 0:
        raise ValueError("filesystem reports no capacity")
    return clamp_percent(100.0 * used / (used + avail))


def read_text(path):
    with open(path) as f:
        return f.read()


def hottest_temperature(raw_values):
    """Hottest in-range zone in degrees C from raw millidegree strings, else None."""
    temps = []
    for raw in raw_values:
        try:
            temps.append(int(raw.strip()) / 1000.0)
        except ValueError:
            pass
    return max((t for t in temps if TEMP_MIN_C <= t <= TEMP_MAX_C), default=None)


def read_temperature(pattern=THERMAL_GLOB):
    raws = []
    for path in sorted(glob.glob(pattern)):
        try:
            raws.append(read_text(path))
        except OSError:  # disabled zones often fail with EIO/EINVAL
            pass
    return hottest_temperature(raws)


def is_valid_device_id(device_id):
    return DEVICE_ID_RE.fullmatch(device_id) is not None


def parse_config(argv=None, env=None):
    """Flag > env var > default. Exits non-zero with a clear message on bad values."""
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(
        description="Push health telemetry for one device to an OTLP/gRPC collector.",
        epilog="Precedence: flag, then environment variable, then default.")
    ap.add_argument("--device-id", help="unique device id (env DEVICE_ID, default: lowercased hostname)")
    ap.add_argument("--endpoint", help="collector host:port, plaintext gRPC (env OTLP_ENDPOINT, default localhost:4317)")
    ap.add_argument("--interval", help="push interval in seconds (env PUSH_INTERVAL, default 5)")
    ap.add_argument("--disk-path", help="path whose filesystem usage is reported (env DISK_PATH, default /)")
    ap.add_argument("--dry-run", action="store_true", help="print one JSON snapshot to stdout and exit; send nothing")
    a = ap.parse_args(argv)

    def pick(flag, name, default):  # an empty env var counts as unset
        return flag if flag is not None else (env.get(name) or default)

    device_id = pick(a.device_id, "DEVICE_ID", socket.gethostname().lower())
    if not is_valid_device_id(device_id):
        ap.error(f"invalid device id {device_id!r}: must match {DEVICE_ID_RE.pattern} "
                 "(set --device-id or DEVICE_ID)")
    endpoint = pick(a.endpoint, "OTLP_ENDPOINT", "localhost:4317").strip().removeprefix("http://").rstrip("/")
    if not endpoint or endpoint.startswith("https://"):
        ap.error("endpoint must be host:port for plaintext gRPC (https:// is not supported)")
    try:
        interval = float(pick(a.interval, "PUSH_INTERVAL", "5"))
    except ValueError:
        interval = math.nan
    if not (math.isfinite(interval) and interval > 0):
        ap.error("interval must be a positive number of seconds")
    return argparse.Namespace(device_id=device_id, endpoint=endpoint, interval=interval,
                              disk_path=pick(a.disk_path, "DISK_PATH", "/"), dry_run=a.dry_run)


class Sampler:
    """Reads the five metrics. A failing collector yields None and never raises."""

    def __init__(self, disk_path, thermal_glob=THERMAL_GLOB):
        self.disk_path, self.thermal_glob = disk_path, thermal_glob
        self._failed = set()
        self._cpu_prev = self._cpu_last = None
        self.read("device_cpu_utilization_percent")  # prime the CPU baseline
        if read_temperature(thermal_glob) is None:
            log.info("no readable temperature sensor; device_temperature_celsius will not be reported")

    def _cpu(self):
        cur = parse_cpu_times(read_text("/proc/stat"))
        prev = self._cpu_prev
        if prev is None or cur[1] < prev[1]:  # first read, or counters went backwards
            self._cpu_prev = cur
            return self._cpu_last
        pct = cpu_percent(prev, cur)
        if pct is not None:  # no jiffies elapsed: keep the old baseline and last value
            self._cpu_prev, self._cpu_last = cur, pct
        return self._cpu_last

    def read(self, name):
        collectors = {
            "device_cpu_utilization_percent": self._cpu,
            "device_memory_utilization_percent": lambda: parse_meminfo_percent(read_text("/proc/meminfo")),
            "device_disk_utilization_percent": lambda: disk_percent(os.statvfs(self.disk_path)),
            "device_uptime_seconds": lambda: parse_uptime(read_text("/proc/uptime")),
            "device_temperature_celsius": lambda: read_temperature(self.thermal_glob),
        }
        try:
            value = collectors[name]()
        except Exception as exc:  # one broken collector must not affect the others
            if name not in self._failed:  # log once, not every cycle
                self._failed.add(name)
                log.warning("%s unavailable: %s", name, exc)
            return None
        self._failed.discard(name)
        return None if value is None else round(value, 2)

    def snapshot(self):
        values = {name: self.read(name) for name in METRICS}
        return {name: v for name, v in values.items() if v is not None}


def run(cfg, sampler, attrs):
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    from opentelemetry.metrics import Observation
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource

    resource = Resource.create({"service.name": "device-agent", "service.instance.id": cfg.device_id})
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=cfg.endpoint, insecure=True),
        export_interval_millis=int(cfg.interval * 1000))
    provider = MeterProvider(resource=resource, metric_readers=[reader])
    meter = provider.get_meter("device-agent")

    def make_callback(name):
        def callback(options):  # runs on every export; no value means no data point
            value = sampler.read(name)
            if value is not None:
                yield Observation(value, attrs)
        return callback

    for name in METRICS:  # no unit on purpose: the Collector would append a name suffix
        meter.create_observable_gauge(name, callbacks=[make_callback(name)])
    meter.create_observable_gauge(INTERVAL_METRIC, callbacks=[lambda options: [Observation(cfg.interval, attrs)]])

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    log.info("device-agent started: device_id=%s endpoint=%s interval=%gs",
             cfg.device_id, cfg.endpoint, cfg.interval)
    stop.wait()
    log.info("shutting down")
    provider.shutdown(timeout_millis=2000)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = parse_config(argv)
    attrs = {"device_id": cfg.device_id, "hostname": socket.gethostname()}
    sampler = Sampler(cfg.disk_path)
    if cfg.dry_run:
        time.sleep(0.5)  # give the CPU delta a window to measure
        resource = {"service.name": "device-agent", "service.instance.id": cfg.device_id}
        metrics = {**sampler.snapshot(), INTERVAL_METRIC: cfg.interval}
        print(json.dumps({"resource": resource, "labels": attrs, "metrics": metrics}, indent=2))
    else:
        run(cfg, sampler, attrs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
