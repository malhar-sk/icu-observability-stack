#!/usr/bin/env python3
"""Watcher: keeps an eye on things that cannot (or should not) run the agent, and writes the same device_* metrics,
so a router, a NAS, a service or an ESPHome board shows up in the Device Fleet dashboard with the same
ONLINE/OFFLINE state and the same DeviceOffline alert.

Four kinds of target (the list lives in a JSON file that `./fleet watch add` manages):

    ping     host                       answers an ICMP echo
    tcp      host:port                  accepts a TCP connection (a PLC on 502, a NAS on 445, SSH on 22)
    http     http(s)://host/path        answers with a good status (2xx or 3xx, or the one you expect)
    metrics  http(s)://host/metrics     serves Prometheus text; its numbers are stored too (ESPHome, Klipper, node_exporter)

Per target and check the watcher writes (stamped on arrival, labels device_id, job="watcher", kind):
    device_probe_success, device_probe_duration_seconds, device_report_interval_seconds      every time
    device_uptime_seconds      only when the check succeeded: seconds the target has answered CONTINUOUSLY (observed uptime)
    device_http_status_code    http and metrics kinds, when an HTTP answer came back
So a target that stops answering stops producing uptime samples and goes OFFLINE after 3 x its interval (at least 30 s),
exactly like an agent that went quiet. See docs/CONTRACT.md and docs/WATCH.md. Standard library only.
"""
import argparse
import concurrent.futures
import http.client
import ipaddress
import json
import logging
import math
import os
import re
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("watcher")

JOB = "watcher"
KINDS = ("ping", "tcp", "http", "metrics")
# One rule in two places (agent/device_agent.py has the same pattern); tests/test_watcher.py keeps them equal.
DEVICE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,251}[A-Za-z0-9])?$")
NAME_RE = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
LABEL_NAME_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
NUMBER_RE = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?|[+-]?Inf|NaN", re.IGNORECASE)
RESERVED_LABELS = ("device_id", "job", "kind")  # ours; a scraped label with one of these names is renamed exported_<name>
DEFAULT_INTERVAL, DEFAULT_TIMEOUT = 15, 5
MIN_INTERVAL, MAX_INTERVAL = 5, 3600
MAX_TARGETS = 100
DEFAULT_MAX_SERIES = 2000  # scraped series kept per target and check: above VictoriaMetrics' own 1199, a typical exporter
MAX_SERIES_LIMIT = 10000   # the most a target may ask for
MAX_BODY = 5 * 1024 * 1024
MAX_LABELS, MAX_LABEL_VALUE = 32, 500
MAX_BUFFER = 20000       # lines kept while VictoriaMetrics is unreachable; the oldest are dropped
MAX_WARNINGS = 200


# ------------------------------------------------------------------ the target list
def _host_ok(host):
    if HOST_RE.fullmatch(host):
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _split_host_port(text):
    """'host:port' or '[v6]:port' -> (host, port); ValueError when it is neither."""
    if text.startswith("["):
        host, sep, port = text[1:].partition("]:")
        if not sep:
            raise ValueError("write an IPv6 address as [address]:port")
    else:
        host, sep, port = text.rpartition(":")
        if not sep or ":" in host:
            raise ValueError("write it as host:port")
    if not _host_ok(host) or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"'{text}' is not a host and a port (1 to 65535)")
    return host, int(port)


def validate_target(raw):
    """A target as stored in the JSON file -> the normalised dict; ValueError (with a message for a person) otherwise."""
    if not isinstance(raw, dict):
        raise ValueError("a target must be an object")
    device_id, kind, target = raw.get("id"), raw.get("kind"), raw.get("target")
    if not isinstance(device_id, str) or not DEVICE_ID_RE.fullmatch(device_id):
        raise ValueError(f"invalid id {device_id!r}: use lowercase letters, digits, '.', '_' and '-' (max 63)")
    if kind not in KINDS:
        raise ValueError(f"{device_id}: kind must be one of {', '.join(KINDS)}")
    if not isinstance(target, str) or not target or len(target) > 2000 or any(c.isspace() or ord(c) < 32 for c in target):
        raise ValueError(f"{device_id}: the target must be one word, without spaces")
    interval = raw.get("interval", DEFAULT_INTERVAL)
    timeout = raw.get("timeout", min(DEFAULT_TIMEOUT, interval) if isinstance(interval, (int, float)) else DEFAULT_TIMEOUT)
    for name, value in (("interval", interval), ("timeout", timeout)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{device_id}: {name} must be a number of seconds")
    if not MIN_INTERVAL <= interval <= MAX_INTERVAL:
        raise ValueError(f"{device_id}: the interval must be between {MIN_INTERVAL} and {MAX_INTERVAL} seconds")
    if not 0 < timeout <= interval:
        raise ValueError(f"{device_id}: the timeout must be above 0 and not longer than the interval")
    insecure, expect = raw.get("insecure", False), raw.get("expect_status")
    max_series = raw.get("max_series")
    if max_series is not None and (isinstance(max_series, bool) or not isinstance(max_series, int)
                                   or not 1 <= max_series <= MAX_SERIES_LIMIT):
        raise ValueError(f"{device_id}: max_series must be a whole number from 1 to {MAX_SERIES_LIMIT}")
    if max_series is not None and kind != "metrics":
        raise ValueError(f"{device_id}: max_series only applies to metrics targets")
    if not isinstance(insecure, bool):
        raise ValueError(f"{device_id}: insecure must be true or false")
    if expect is not None and (isinstance(expect, bool) or not isinstance(expect, int) or not 100 <= expect <= 599):
        raise ValueError(f"{device_id}: expect_status must be an HTTP status such as 200")
    if kind in ("ping", "tcp") and (insecure or expect is not None):
        raise ValueError(f"{device_id}: insecure and expect_status only apply to http and metrics targets")
    if kind == "metrics" and expect is not None:
        raise ValueError(f"{device_id}: a metrics target must answer 200; expect_status does not apply")
    if kind == "ping":
        if not _host_ok(target):
            raise ValueError(f"{device_id}: '{target}' is not a host name or an IP address")
    elif kind == "tcp":
        try:
            _split_host_port(target)
        except ValueError as exc:
            raise ValueError(f"{device_id}: {exc}") from None
    else:
        parts = urllib.parse.urlsplit(target)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"{device_id}: '{target}' must be an http:// or https:// address")
        if parts.username or parts.password:
            raise ValueError(f"{device_id}: a login in the address is not supported")
        try:
            parts.port
        except ValueError:
            raise ValueError(f"{device_id}: the port in '{target}' is not valid") from None
        if insecure and parts.scheme != "https":
            raise ValueError(f"{device_id}: insecure only applies to https addresses")
    return {"id": device_id, "kind": kind, "target": target, "interval": interval, "timeout": timeout,
            "insecure": insecure, "expect_status": expect,
            "max_series": (max_series or DEFAULT_MAX_SERIES) if kind == "metrics" else None}


def load_targets(path):
    """(valid targets, problems). A missing file is simply no targets; a bad entry is reported and skipped."""
    try:
        with open(path) as f:
            doc = json.load(f)
    except FileNotFoundError:
        return [], []
    except (OSError, ValueError) as exc:
        return None, [f"cannot read {path}: {exc}"]
    raw = doc.get("targets") if isinstance(doc, dict) else None
    if not isinstance(raw, list):
        return None, [f"{path}: expected an object with a 'targets' list"]
    targets, problems, seen = [], [], set()
    for entry in raw[:MAX_TARGETS + 1]:
        try:
            target = validate_target(entry)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        if target["id"] in seen:
            problems.append(f"{target['id']}: listed twice, the first one is used")
        elif len(targets) >= MAX_TARGETS:
            problems.append(f"more than {MAX_TARGETS} targets: the rest are ignored")
            break
        else:
            seen.add(target["id"])
            targets.append(target)
    return targets, problems


# ------------------------------------------------------------------ the checks
class Result:
    """What one check found."""

    def __init__(self, ok, duration, detail, status=None, samples=None, extra=None):
        self.ok, self.duration, self.detail, self.status = ok, duration, detail, status
        self.samples = samples      # scraped (name, labels, value) triples, metrics kind only
        self.extra = extra or {}    # notes for the log: dropped counts


def probe_tcp(host, port, timeout):
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except socket.timeout:
        return Result(False, time.monotonic() - start, "timed out")
    except socket.gaierror as exc:
        return Result(False, time.monotonic() - start, f"cannot resolve {host} ({exc.strerror or exc})")
    except OSError as exc:
        return Result(False, time.monotonic() - start, exc.strerror or str(exc))
    return Result(True, time.monotonic() - start, "connected")


def probe_ping(host, timeout, run=subprocess.run):
    """One ICMP echo through the system `ping` (the host name is validated: it cannot start with '-')."""
    start = time.monotonic()
    cmd = ["ping", "-c", "1", "-W", str(max(1, math.ceil(timeout))), host]
    try:
        proc = run(cmd, capture_output=True, text=True, timeout=timeout + 3)
    except FileNotFoundError:
        return Result(False, 0.0, "the 'ping' program is not installed")
    except subprocess.TimeoutExpired:
        return Result(False, time.monotonic() - start, "timed out")
    elapsed = time.monotonic() - start
    if proc.returncode == 0:
        m = re.search(r"time[=<]([\d.]+)\s*ms", proc.stdout)
        return Result(True, float(m.group(1)) / 1000 if m else elapsed, f"{float(m.group(1)):g} ms" if m else "replied")
    text = (proc.stdout + proc.stderr).lower()
    if "bad address" in text or "unknown host" in text or "name or service not known" in text:
        return Result(False, elapsed, f"cannot resolve {host}")
    return Result(False, elapsed, "no reply")


def _fetch(url, timeout, insecure, max_bytes, want_body):
    """(status, body or None, problem or None). An HTTP error status is an answer, not a problem."""
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))
    request = urllib.request.Request(url, headers={"Accept": "text/plain;version=0.0.4;q=1,*/*;q=0.1",
                                                    "User-Agent": "fleet-watcher"})
    try:
        with opener.open(request, timeout=timeout) as response:
            status, body = response.status, None
            if want_body:
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    return status, None, f"the answer is larger than {max_bytes // (1024 * 1024)} MB"
                declared = response.headers.get("Content-Length")
                if declared and declared.isdigit() and len(body) < int(declared):
                    return status, None, "the answer was cut short"  # a partial scrape must not be stored as if whole
            return status, body, None
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code, None, None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            return None, None, f"the certificate was refused ({reason.verify_message}); for a self-signed one add --insecure"
        if isinstance(reason, socket.gaierror):
            return None, None, f"cannot resolve the host name ({reason.strerror or reason})"
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return None, None, "timed out"
        return None, None, str(getattr(reason, "strerror", None) or reason)
    except (socket.timeout, TimeoutError):
        return None, None, "timed out"
    except (OSError, ValueError, http.client.HTTPException) as exc:  # a connection cut mid-way, a malformed answer
        return None, None, str(exc) or exc.__class__.__name__


def probe_http(url, timeout, insecure=False, expect_status=None):
    start = time.monotonic()
    status, _, problem = _fetch(url, timeout, insecure, 0, want_body=False)
    elapsed = time.monotonic() - start
    if problem:
        return Result(False, elapsed, problem)
    good = status == expect_status if expect_status else 200 <= status < 400
    want = f" (expected {expect_status})" if expect_status and not good else ""
    return Result(good, elapsed, f"HTTP {status}{want}", status=status)


def probe_metrics(url, timeout, insecure=False, max_series=DEFAULT_MAX_SERIES):
    start = time.monotonic()
    status, body, problem = _fetch(url, timeout, insecure, MAX_BODY, want_body=True)
    elapsed = time.monotonic() - start
    if problem:
        return Result(False, elapsed, problem, status=status)
    if status != 200:
        return Result(False, elapsed, f"HTTP {status}", status=status)
    samples, stats = parse_metrics(body.decode("utf-8", errors="replace"), max_series)
    if not samples:
        why = ("it is not Prometheus text (a web page?)" if stats["bad_lines"]
               else "every metric was reserved or not a number" if stats["reserved"] or stats["non_finite"]
               else "no metrics in the answer")
        return Result(False, elapsed, f"HTTP 200, but {why}", status=status)
    return Result(True, time.monotonic() - start, f"HTTP 200, {len(samples)} series", status=status,
                  samples=samples, extra=stats)


# ------------------------------------------------------------------ Prometheus text
def _parse_labels(line, pos):
    """Labels after '{' -> ({name: value}, position after '}'). ValueError for anything malformed."""
    labels = {}
    while True:
        while pos < len(line) and line[pos] in " \t":
            pos += 1
        if pos >= len(line):
            raise ValueError("unterminated label set")
        if line[pos] == "}":
            return labels, pos + 1
        m = LABEL_NAME_RE.match(line, pos)
        if not m:
            raise ValueError("bad label name")
        name, pos = m.group(0), m.end()
        if line[pos:pos + 2] != '="':
            raise ValueError("expected =\"")
        pos += 2
        chars = []
        while True:
            if pos >= len(line):
                raise ValueError("unterminated label value")
            ch = line[pos]
            if ch == "\\" and pos + 1 < len(line):
                nxt = line[pos + 1]
                chars.append({"n": "\n", "\\": "\\", '"': '"'}.get(nxt, "\\" + nxt))
                pos += 2
            elif ch == '"':
                pos += 1
                break
            else:
                chars.append(ch)
                pos += 1
        if len(chars) > MAX_LABEL_VALUE or len(labels) >= MAX_LABELS:
            raise ValueError("label too long or too many labels")
        labels[name] = "".join(chars)
        while pos < len(line) and line[pos] in " \t":
            pos += 1
        if pos < len(line) and line[pos] == ",":
            pos += 1


def parse_sample(line):
    """'name{a="b"} 1.5 [timestamp]' -> (name, labels, value). ValueError when it is not a sample."""
    m = NAME_RE.match(line)
    if not m:
        raise ValueError("no metric name")
    name, pos, labels = m.group(0), m.end(), {}
    if pos < len(line) and line[pos] == "{":
        labels, pos = _parse_labels(line, pos + 1)
    rest = line[pos:].split()
    if not rest or not NUMBER_RE.fullmatch(rest[0]):
        raise ValueError("no value")
    return name, labels, float(rest[0])  # the exporter's own timestamp (rest[1]) is ignored: arrival time counts


def parse_metrics(text, max_series=DEFAULT_MAX_SERIES):
    """Prometheus text -> (samples, stats). Never raises. Names starting with device_ are ours and are dropped, non-finite
    values are dropped, and at most max_series are kept (the first ones in the order the page lists them)."""
    samples, stats = [], {"bad_lines": 0, "reserved": 0, "non_finite": 0, "over_limit": 0}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            name, labels, value = parse_sample(line)
        except ValueError:
            stats["bad_lines"] += 1
            continue
        if name.startswith("device_"):
            stats["reserved"] += 1
        elif not math.isfinite(value):
            stats["non_finite"] += 1
        elif len(samples) >= max_series:
            stats["over_limit"] += 1
        else:
            samples.append((name, labels, value))
    return samples, stats


def _escape(value):
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def format_line(name, labels, value, stamp_ms):
    body = ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items())
    return f"{name}{{{body}}} {value!r} {stamp_ms}"


# ------------------------------------------------------------------ the watcher
class Pusher:
    """Delivers Prometheus text lines to VictoriaMetrics from a bounded buffer, and logs a problem once rather than every
    second. The watcher and the check-in service (watcher/checkin.py) both use it. Thread safe."""

    def __init__(self, vm_url):
        self.vm_url = vm_url.rstrip("/")
        self.lock = threading.Lock()
        self.pending = []
        self.warned = set()
        self.vm_ok = True
        self.stats = {"delivered": 0, "dropped_unsent": 0}
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # a local service: never via a proxy

    def warn_once(self, key, message, *args):
        if key in self.warned or len(self.warned) >= MAX_WARNINGS:
            return
        self.warned.add(key)
        log.warning(message, *args)

    def _extend_locked(self, lines):
        """Add lines to the buffer (the caller holds the lock); the oldest go first once it is full."""
        self.pending.extend(lines)
        overflow = len(self.pending) - MAX_BUFFER
        if overflow > 0:
            del self.pending[:overflow]
            self.stats["dropped_unsent"] += overflow

    def add_lines(self, lines):
        with self.lock:
            self._extend_locked(lines)

    def flush(self):
        with self.lock:
            lines, self.pending = self.pending, []
        if not lines:
            return 0
        body = ("\n".join(lines) + "\n").encode()
        request = urllib.request.Request(self.vm_url + "/api/v1/import/prometheus", data=body, method="POST")
        try:
            with self._opener.open(request, timeout=10) as response:
                response.read()
        except (urllib.error.URLError, OSError) as exc:
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            with self.lock:
                self.pending = lines + self.pending
                overflow = len(self.pending) - MAX_BUFFER
                if overflow > 0:
                    del self.pending[:overflow]
                    self.stats["dropped_unsent"] += overflow
            raise
        self.stats["delivered"] += len(lines)
        return len(lines)

    def flush_logged(self):
        try:
            sent = self.flush()
        except (urllib.error.URLError, OSError) as exc:
            if self.vm_ok:
                log.error("cannot reach VictoriaMetrics at %s (%s); keeping results and retrying", self.vm_url, exc)
            self.vm_ok = False
            return 0
        if not self.vm_ok:
            log.info("VictoriaMetrics is reachable again")
            self.vm_ok = True
        return sent


class Watcher(Pusher):
    """Runs the checks that are due, turns the results into metric lines, and delivers them to VictoriaMetrics.
    Thread safe: checks run in a pool while the main loop flushes."""

    def __init__(self, vm_url, clock=time.time, probes=None):
        super().__init__(vm_url)
        self.clock = clock
        self.probes = probes or {}      # kind -> function(spec) -> Result; tests replace it
        self.targets = {}               # id -> spec
        self.state = {}                 # id -> {"since": streak start or None, "up": last outcome or None}
        self.next_due = {}
        self.inflight = set()
        self.stats.update(checks=0, failed=0)

    # ---- the list of targets
    def apply_targets(self, specs):
        with self.lock:
            new = {s["id"]: s for s in specs}
            for gone in set(self.targets) - set(new):
                log.info("%s: no longer watched", gone)
                for table in (self.state, self.next_due):
                    table.pop(gone, None)
            for device_id, spec in new.items():
                if self.targets.get(device_id) != spec:
                    if device_id in self.targets:
                        log.info("%s: settings changed", device_id)
                    self.state[device_id] = {"since": None, "up": None}
                    self.next_due[device_id] = 0.0
                    self.warned = {k for k in self.warned if not (isinstance(k, tuple) and k[1] == device_id)}
            self.targets = new

    def due(self, now):
        """The targets whose turn has come; each is marked in flight and rescheduled at its own interval."""
        out = []
        with self.lock:
            for device_id, spec in self.targets.items():
                if device_id not in self.inflight and now >= self.next_due.get(device_id, 0.0):
                    self.inflight.add(device_id)
                    self.next_due[device_id] = now + spec["interval"]
                    out.append(spec)
        return out

    # ---- one check
    def check(self, spec):
        kind, target = spec["kind"], spec["target"]
        if kind in self.probes:
            return self.probes[kind](spec)
        if kind == "ping":
            return probe_ping(target, spec["timeout"])
        if kind == "tcp":
            host, port = _split_host_port(target)
            return probe_tcp(host, port, spec["timeout"])
        if kind == "http":
            return probe_http(target, spec["timeout"], spec["insecure"], spec["expect_status"])
        return probe_metrics(target, spec["timeout"], spec["insecure"], spec.get("max_series") or DEFAULT_MAX_SERIES)

    def run_check(self, spec):
        """Run one check and record it. Never raises: a broken check must not stop the others."""
        try:
            result = self.check(spec)
        except Exception as exc:  # a bug in a check shows up as a failed check, in the log
            log.exception("%s: the check itself failed", spec["id"])
            result = Result(False, 0.0, f"internal error: {exc.__class__.__name__}")
        finally:
            with self.lock:
                self.inflight.discard(spec["id"])
        self.record(spec, result)

    def record(self, spec, result):
        now = self.clock()
        stamp = int(now * 1000)
        device_id = spec["id"]
        base = {"device_id": device_id, "job": JOB, "kind": spec["kind"]}
        lines = [format_line("device_probe_success", base, 1.0 if result.ok else 0.0, stamp),
                 format_line("device_probe_duration_seconds", base, round(result.duration, 6), stamp),
                 format_line("device_report_interval_seconds", base, float(spec["interval"]), stamp)]
        if result.status is not None:
            lines.append(format_line("device_http_status_code", base, float(result.status), stamp))
        with self.lock:
            if device_id not in self.targets:  # removed while the check was running: write nothing
                return
            state = self.state.setdefault(device_id, {"since": None, "up": None})
            self.stats["checks"] += 1
            if result.ok:
                if state["since"] is None:
                    state["since"] = now
                lines.append(format_line("device_uptime_seconds", base, round(now - state["since"], 3), stamp))
                if result.samples is not None:
                    for name, labels, value in result.samples:
                        merged = {(f"exported_{k}" if k in RESERVED_LABELS else k): v for k, v in labels.items()}
                        merged.update(base)
                        lines.append(format_line(name, merged, value, stamp))
                    lines.append(format_line("device_scrape_series", base, float(len(result.samples)), stamp))
            else:
                state["since"] = None
                self.stats["failed"] += 1
            changed = state["up"] != result.ok
            state["up"] = result.ok
            self._extend_locked(lines)
        what = f"{spec['kind']} {spec['target']}"
        if changed:
            (log.info if result.ok else log.warning)("%s: %s (%s: %s)", device_id, "UP" if result.ok else "DOWN", what, result.detail)
        for key, text in (("reserved", "metric names starting with device_ are reserved and were dropped"),
                          ("non_finite", "NaN and infinite values were dropped"),
                          ("over_limit", f"more than {spec.get('max_series') or DEFAULT_MAX_SERIES} series: the rest were dropped "
                                         "(raise it with --max-series)"),
                          ("bad_lines", "lines that are not Prometheus text were skipped")):
            if result.extra.get(key):
                self.warn_once((key, device_id), "%s: %s (%d in the latest check)", device_id, text, result.extra[key])


# ------------------------------------------------------------------ the service
def parse_config(argv=None, env=None):
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(description="Check pings, ports, web pages and metrics endpoints and report them as devices.")
    ap.add_argument("--targets", help="the JSON file of targets (env TARGETS_FILE, default /watch/targets.json)")
    ap.add_argument("--vm-url", help="VictoriaMetrics address (env VM_URL, default http://victoriametrics:8428)")
    ap.add_argument("--once", action="store_true", help="check every target once, send the results and exit")
    a = ap.parse_args(argv)
    return argparse.Namespace(targets=a.targets or env.get("TARGETS_FILE") or "/watch/targets.json",
                              vm_url=a.vm_url or env.get("VM_URL") or "http://victoriametrics:8428", once=a.once)


def _mtime(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def run(cfg):
    watcher = Watcher(cfg.vm_url)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="check")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    seen, reported = object(), set()
    log.info("starting: targets in %s, VictoriaMetrics %s", cfg.targets, cfg.vm_url)
    last_stats = time.time()
    while not stop.is_set():
        stamp = _mtime(cfg.targets)
        if stamp != seen:
            seen = stamp
            specs, problems = load_targets(cfg.targets)
            for problem in problems:
                if problem not in reported:
                    reported.add(problem)
                    log.error("targets file: %s", problem)
            if specs is not None:
                watcher.apply_targets(specs)
                log.info("watching %d target(s)", len(specs))
        futures = [pool.submit(watcher.run_check, spec) for spec in watcher.due(time.time())]
        if cfg.once:
            concurrent.futures.wait(futures)
            watcher.flush_logged()
            break
        watcher.flush_logged()
        if time.time() - last_stats >= 3600:
            last_stats = time.time()
            log.info("last hour: %s", ", ".join(f"{k}={v}" for k, v in watcher.stats.items()))
        stop.wait(0.5)
    pool.shutdown(wait=True)
    watcher.flush_logged()
    log.info("stopped")


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(parse_config(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
