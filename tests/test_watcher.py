"""Tests for watcher/watcher.py. Stdlib only. The TCP and HTTP checks run against real local servers; ping uses a fake
`ping` program result; a fake VictoriaMetrics HTTP server records what is delivered. The live path (real containers, a
real exporter, the real alert) is covered by scripts/validate_watch.sh and described in docs/VALIDATION.md.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("watcher", os.path.join(ROOT, "watcher", "watcher.py"))
w = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(w)
w.log.addHandler(logging.NullHandler())  # keeps the watcher's log lines out of the test output


class Capture(logging.Handler):
    def __init__(self, level=logging.INFO):
        super().__init__(level)
        self.records = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))

    def __enter__(self):
        w.log.setLevel(logging.INFO)
        w.log.addHandler(self)
        return self

    def __exit__(self, *exc):
        w.log.removeHandler(self)

    @property
    def messages(self):
        return [m for _, m in self.records]


def good(**over):
    base = {"id": "router", "kind": "ping", "target": "192.168.1.1"}
    base.update(over)
    return base


class ValidateTargetTests(unittest.TestCase):
    def test_defaults_are_filled_in(self):
        t = w.validate_target(good())
        self.assertEqual((t["interval"], t["timeout"], t["insecure"], t["expect_status"], t["max_series"]), (15, 5, False, None, None))
        self.assertEqual(w.validate_target(good(kind="metrics", target="http://x/m"))["max_series"], w.DEFAULT_MAX_SERIES)

    def test_good_targets_of_every_kind(self):
        for kind, target in [("ping", "192.168.1.1"), ("ping", "nas.lan"), ("ping", "fe80::1"), ("ping", "pi-zero_2"),
                             ("tcp", "192.168.1.20:502"), ("tcp", "nas.lan:445"), ("tcp", "[::1]:22"),
                             ("http", "http://pihole.lan/admin/"), ("http", "https://10.0.0.1:8443/status?x=1"),
                             ("metrics", "http://esp32.lan/metrics"), ("metrics", "https://exporter:9100/metrics")]:
            self.assertEqual(w.validate_target(good(kind=kind, target=target))["target"], target)

    def test_interval_and_timeout_rules(self):
        self.assertEqual(w.validate_target(good(interval=5))["timeout"], 5)  # the default timeout never exceeds the interval
        self.assertEqual(w.validate_target(good(interval=60, timeout=10))["timeout"], 10)
        for bad in [dict(interval=4), dict(interval=3601), dict(interval=0), dict(interval=-1), dict(interval="15"),
                    dict(interval=True), dict(interval=float("nan")), dict(interval=float("inf")),
                    dict(interval=10, timeout=11), dict(timeout=0), dict(timeout=-2), dict(timeout="5")]:
            with self.assertRaises(ValueError, msg=str(bad)):
                w.validate_target(good(**bad))

    def test_bad_ids_and_kinds(self):
        for bad in [dict(id="Router"), dict(id="-x"), dict(id="a b"), dict(id=""), dict(id=None), dict(id="a" * 64),
                    dict(kind="udp"), dict(kind=None), dict(kind="PING")]:
            with self.assertRaises(ValueError, msg=str(bad)):
                w.validate_target(good(**bad))

    def test_bad_targets(self):
        for kind, bad in [("ping", ""), ("ping", "-c 100 x"), ("ping", "-f"), ("ping", "a b"), ("ping", "host;rm"),
                          ("ping", "$(id)"), ("ping", "http://x"), ("ping", "a" * 300), ("ping", "x\ny"),
                          ("tcp", "host"), ("tcp", "host:"), ("tcp", "host:0"), ("tcp", "host:70000"), ("tcp", "host:ab"),
                          ("tcp", "a:b:c"), ("tcp", "[::1]"), ("tcp", "-x:80"),
                          ("http", "ftp://x/"), ("http", "x.lan/"), ("http", "http://"), ("http", "http://u:p@x/"),
                          ("http", "http://x:abc/"), ("http", "http://x:99999/"), ("http", "http://x/ y"),
                          ("metrics", "file:///etc/passwd"), ("metrics", "javascript:alert(1)")]:
            with self.assertRaises(ValueError, msg=f"{kind} {bad!r}"):
                w.validate_target(good(kind=kind, target=bad))

    def test_options_only_where_they_make_sense(self):
        self.assertTrue(w.validate_target(good(kind="http", target="https://x/", insecure=True))["insecure"])
        self.assertEqual(w.validate_target(good(kind="http", target="http://x/", expect_status=401))["expect_status"], 401)
        for kind, target, extra in [("ping", "x", dict(insecure=True)), ("tcp", "x:1", dict(insecure=True)),
                                    ("ping", "x", dict(expect_status=200)), ("http", "http://x/", dict(insecure=True)),
                                    ("http", "https://x/", dict(insecure="yes")), ("http", "http://x/", dict(expect_status=99)),
                                    ("http", "http://x/", dict(expect_status=True)), ("metrics", "http://x/m", dict(expect_status=200))]:
            with self.assertRaises(ValueError, msg=f"{kind} {extra}"):
                w.validate_target(good(kind=kind, target=target, **extra))

    def test_max_series(self):
        t = w.validate_target(good(kind="metrics", target="http://x/m", max_series=50))
        self.assertEqual(t["max_series"], 50)
        for bad in [0, -1, w.MAX_SERIES_LIMIT + 1, 1.5, "100", True]:
            with self.assertRaises(ValueError, msg=repr(bad)):
                w.validate_target(good(kind="metrics", target="http://x/m", max_series=bad))
        with self.assertRaises(ValueError):
            w.validate_target(good(kind="http", target="http://x/", max_series=10))  # only metrics targets scrape

    def test_the_default_limit_covers_a_real_exporter(self):
        # VictoriaMetrics' own /metrics page has 1199 series; the default must not cut an ordinary exporter
        self.assertGreater(w.DEFAULT_MAX_SERIES, 1199)

    def test_the_id_rule_is_the_agents_rule(self):
        sys.path.insert(0, os.path.join(ROOT, "agent"))
        from device_agent import DEVICE_ID_RE
        self.assertEqual(w.DEVICE_ID_RE.pattern, DEVICE_ID_RE.pattern)

    def test_a_non_object_is_refused(self):
        for bad in [None, [], "router", 5]:
            with self.assertRaises(ValueError):
                w.validate_target(bad)


class LoadTargetsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "targets.json")

    def write(self, doc):
        with open(self.path, "w") as f:
            f.write(doc if isinstance(doc, str) else json.dumps(doc))

    def test_a_missing_file_is_no_targets(self):
        self.assertEqual(w.load_targets(self.path), ([], []))

    def test_valid_targets(self):
        self.write({"version": 1, "targets": [good(), good(id="nas", kind="tcp", target="nas.lan:445", interval=30)]})
        targets, problems = w.load_targets(self.path)
        self.assertEqual([t["id"] for t in targets], ["router", "nas"])
        self.assertEqual(problems, [])

    def test_a_bad_entry_is_reported_and_skipped_without_losing_the_good_ones(self):
        self.write({"targets": [good(), good(id="bad", kind="udp"), good(id="nas", kind="tcp", target="nas.lan:445")]})
        targets, problems = w.load_targets(self.path)
        self.assertEqual([t["id"] for t in targets], ["router", "nas"])
        self.assertEqual(len(problems), 1)
        self.assertIn("bad", problems[0])

    def test_a_broken_file_gives_none_so_the_caller_keeps_what_it_had(self):
        for doc in ["{not json", "[]", json.dumps({"targets": "x"}), json.dumps({"nope": 1}), ""]:
            self.write(doc)
            targets, problems = w.load_targets(self.path)
            self.assertIsNone(targets, doc)
            self.assertTrue(problems)

    def test_duplicates_and_the_limit(self):
        self.write({"targets": [good(), good(target="192.168.1.2")]})
        targets, problems = w.load_targets(self.path)
        self.assertEqual((len(targets), targets[0]["target"]), (1, "192.168.1.1"))
        self.assertIn("twice", problems[0])
        self.write({"targets": [good(id=f"t{i}") for i in range(w.MAX_TARGETS + 5)]})
        targets, problems = w.load_targets(self.path)
        self.assertEqual(len(targets), w.MAX_TARGETS)
        self.assertTrue(any("more than" in p for p in problems))


ESPHOME = """\
# HELP esphome_sensor_value Value of sensor
# TYPE esphome_sensor_value gauge
esphome_sensor_value{id="temperature",name="Temperature",unit="°C"} 21.5
esphome_sensor_value{id="humidity",name="Humidity",unit="%"} 48.25
esphome_sensor_failed{id="temperature",name="Temperature"} 0
# HELP esphome_binary_sensor_value Value of binary sensor
esphome_binary_sensor_value{id="door",name="Door"} 1
"""


class ParseMetricsTests(unittest.TestCase):
    def test_a_real_esphome_answer(self):
        samples, stats = w.parse_metrics(ESPHOME)
        self.assertEqual(len(samples), 4)
        self.assertEqual(samples[0], ("esphome_sensor_value", {"id": "temperature", "name": "Temperature", "unit": "°C"}, 21.5))
        self.assertEqual(stats, {"bad_lines": 0, "reserved": 0, "non_finite": 0, "over_limit": 0})

    def test_sample_forms(self):
        for line, want in [("up 1", ("up", {}, 1.0)),
                           ("up{} 1", ("up", {}, 1.0)),
                           ('m{a="b",} 2', ("m", {"a": "b"}, 2.0)),
                           ('m{a="b", c="d"} 2.5 1700000000000', ("m", {"a": "b", "c": "d"}, 2.5)),  # the timestamp is ignored
                           ('m{a="x\\"y\\\\z\\nw"} 3', ("m", {"a": 'x"y\\z\nw'}, 3.0)),
                           ('m{a="has } brace and {"} 4', ("m", {"a": "has } brace and {"}, 4.0)),
                           ("m 1e3", ("m", {}, 1000.0)), ("m -5", ("m", {}, -5.0)), ("m +.5", ("m", {}, 0.5)),
                           ("ns:rule:rate5m 1", ("ns:rule:rate5m", {}, 1.0)),
                           ('m{a="b"} 7 # {trace_id="x"} 1 1700000000', ("m", {"a": "b"}, 7.0))]:  # an OpenMetrics exemplar
            self.assertEqual(w.parse_sample(line), want, line)

    def test_malformed_lines_raise(self):
        for bad in ["", "{a=\"b\"} 1", "1abc 1", "m", "m x", 'm{a="b} 1', 'm{a=b} 1', 'm{="b"} 1', "m{a=\"b\"", "m 1_0",
                    "m 0x10", "<html>", '{"json": 1}', "OK"]:
            with self.assertRaises(ValueError, msg=repr(bad)):
                w.parse_sample(bad)

    def test_non_finite_values_are_dropped_not_stored(self):
        samples, stats = w.parse_metrics("a NaN\nb +Inf\nc -Inf\nd 1\n")
        self.assertEqual([s[0] for s in samples], ["d"])
        self.assertEqual(stats["non_finite"], 3)

    def test_names_starting_with_device_are_ours_and_are_dropped(self):
        samples, stats = w.parse_metrics("device_uptime_seconds 99999\ndevice_probe_success 1\ndevices_total 3\nup 1\n")
        self.assertEqual([s[0] for s in samples], ["devices_total", "up"])
        self.assertEqual(stats["reserved"], 2)

    def test_the_series_limit(self):
        text = "".join(f'm{{i="{i}"}} 1\n' for i in range(w.DEFAULT_MAX_SERIES + 50))
        samples, stats = w.parse_metrics(text)
        self.assertEqual((len(samples), stats["over_limit"]), (w.DEFAULT_MAX_SERIES, 50))
        samples, stats = w.parse_metrics(text, max_series=10)  # a target may ask for less (or more)
        self.assertEqual((len(samples), stats["over_limit"]), (10, w.DEFAULT_MAX_SERIES + 40))
        self.assertEqual(samples[0][1], {"i": "0"})  # the first ones in page order are the ones kept

    def test_garbage_is_counted_not_raised(self):
        samples, stats = w.parse_metrics("<html>\n<body>hello</body>\nOK\n\n# comment\nreal 1\n")
        self.assertEqual((len(samples), stats["bad_lines"]), (1, 3))

    def test_a_label_flood_is_refused(self):
        many = ",".join(f'l{i}="v"' for i in range(w.MAX_LABELS + 1))
        self.assertEqual(w.parse_metrics(f"m{{{many}}} 1")[1]["bad_lines"], 1)
        self.assertEqual(w.parse_metrics('m{a="' + "x" * (w.MAX_LABEL_VALUE + 1) + '"} 1')[1]["bad_lines"], 1)

    def test_output_is_always_one_line_with_escaped_labels(self):
        line = w.format_line("m", {"a": 'q"uote', "b": "back\\slash", "c": "new\nline"}, 1.5, 1700000000000)
        self.assertNotIn("\n", line)
        self.assertEqual(line, 'm{a="q\\"uote",b="back\\\\slash",c="new\\nline"} 1.5 1700000000000')
        name, labels, value = w.parse_sample(line.rsplit(" ", 1)[0])  # and it reads back as what went in
        self.assertEqual(labels, {"a": 'q"uote', "b": "back\\slash", "c": "new\nline"})


# ---------------------------------------------------------------- real local servers
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path
        if path == "/ok":
            self._send(200, b"fine", "text/plain")
        elif path == "/missing":
            self._send(404, b"nope", "text/plain")
        elif path == "/boom":
            self._send(500, b"oops", "text/plain")
        elif path == "/hop":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/auth":
            self._send(401, b"who?", "text/plain")
        elif path == "/slow":
            time.sleep(2)
            self._send(200, b"late", "text/plain")
        elif path == "/metrics":
            self._send(200, ESPHOME.encode(), "text/plain; version=0.0.4")
        elif path == "/html":
            self._send(200, b"<html><body><h1>Router</h1></body></html>", "text/html")
        elif path == "/empty":
            self._send(200, b"", "text/plain")
        elif path == "/hostile":
            body = b"device_uptime_seconds 123456\nok_metric 1\nbad_value NaN\n"
            self._send(200, body, "text/plain")
        elif path == "/huge":
            body = b"m 1\n" * (w.MAX_BODY // 4 + 10)
            self._send(200, body, "text/plain")
        elif path == "/cut":  # promises more than it sends
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b"m 1\n")
            self.wfile.flush()
        else:
            self._send(404, b"?", "text/plain")

    def _send(self, code, body, ctype):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):  # the client gave up (the timeout tests do)
            pass


class ProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    # tcp
    def test_tcp_open_and_closed(self):
        r = w.probe_tcp("127.0.0.1", self.port, 2)
        self.assertTrue(r.ok)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
        s.close()  # nothing listens here any more
        r = w.probe_tcp("127.0.0.1", free, 2)
        self.assertFalse(r.ok)
        self.assertRegex(r.detail, r"(?i)refused")

    def test_tcp_unresolvable_name(self):
        r = w.probe_tcp("no-such-host.invalid", 80, 2)
        self.assertFalse(r.ok)
        self.assertIn("cannot resolve", r.detail)

    # http
    def test_http_statuses(self):
        for path, ok, status in [("/ok", True, 200), ("/hop", True, 200), ("/missing", False, 404), ("/boom", False, 500),
                                 ("/auth", False, 401)]:
            r = w.probe_http(self.base + path, 3)
            self.assertEqual((r.ok, r.status), (ok, status), path)

    def test_http_expect_status(self):
        self.assertTrue(w.probe_http(self.base + "/auth", 3, expect_status=401).ok)
        r = w.probe_http(self.base + "/ok", 3, expect_status=204)
        self.assertFalse(r.ok)
        self.assertIn("expected 204", r.detail)

    def test_http_timeout_and_refused(self):
        r = w.probe_http(self.base + "/slow", 1)
        self.assertEqual((r.ok, r.detail), (False, "timed out"))
        self.assertFalse(w.probe_http("http://127.0.0.1:1/", 2).ok)
        self.assertIn("cannot resolve", w.probe_http("http://no-such-host.invalid/", 2).detail)

    def test_http_ignores_proxy_settings(self):
        old = {k: os.environ.get(k) for k in ("HTTP_PROXY", "http_proxy")}
        os.environ["HTTP_PROXY"] = os.environ["http_proxy"] = "http://127.0.0.1:9"
        try:
            self.assertTrue(w.probe_http(self.base + "/ok", 3).ok)
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    # metrics
    def test_metrics_scrape(self):
        r = w.probe_metrics(self.base + "/metrics", 3)
        self.assertTrue(r.ok)
        self.assertEqual((len(r.samples), r.status), (4, 200))
        self.assertIn("4 series", r.detail)

    def test_a_web_page_is_not_a_metrics_endpoint(self):
        r = w.probe_metrics(self.base + "/html", 3)
        self.assertFalse(r.ok)
        self.assertIn("not Prometheus text", r.detail)
        self.assertFalse(w.probe_metrics(self.base + "/empty", 3).ok)

    def test_metrics_wrong_status_and_hostile_content(self):
        self.assertEqual(w.probe_metrics(self.base + "/missing", 3).detail, "HTTP 404")
        r = w.probe_metrics(self.base + "/hostile", 3)
        self.assertTrue(r.ok)
        self.assertEqual([s[0] for s in r.samples], ["ok_metric"])
        self.assertEqual((r.extra["reserved"], r.extra["non_finite"]), (1, 1))

    def test_an_oversized_answer_is_refused(self):
        r = w.probe_metrics(self.base + "/huge", 10)
        self.assertFalse(r.ok)
        self.assertIn("larger than", r.detail)

    def test_a_truncated_answer_is_a_failed_check_not_a_crash(self):
        r = w.probe_metrics(self.base + "/cut", 3)
        self.assertFalse(r.ok)
        self.assertIn("cut short", r.detail)

    # ping
    def test_ping_outcomes(self):
        def run_with(code, out="", err=""):
            return lambda cmd, **kw: subprocess.CompletedProcess(cmd, code, out, err)
        r = w.probe_ping("192.168.1.1", 5, run=run_with(0, "64 bytes from 192.168.1.1: seq=0 ttl=64 time=2.349 ms\n"))
        self.assertEqual((r.ok, r.detail), (True, "2.349 ms"))
        self.assertAlmostEqual(r.duration, 0.002349)
        self.assertEqual(w.probe_ping("x", 5, run=run_with(1, "1 packets transmitted, 0 packets received")).detail, "no reply")
        self.assertIn("cannot resolve", w.probe_ping("x", 5, run=run_with(1, "", "ping: bad address 'x'")).detail)

        def missing(cmd, **kw):
            raise FileNotFoundError

        def slow(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 1)
        self.assertIn("not installed", w.probe_ping("x", 5, run=missing).detail)
        self.assertEqual(w.probe_ping("x", 5, run=slow).detail, "timed out")

    def test_ping_command_is_safe_and_uses_whole_seconds(self):
        seen = []

        def spy(cmd, **kw):
            seen.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, 0, "time=1 ms", "")
        w.probe_ping("nas.lan", 2.2, run=spy)
        cmd, kw = seen[0]
        self.assertEqual(cmd, ["ping", "-c", "1", "-W", "3", "nas.lan"])
        self.assertNotIn("shell", kw)  # the host name is an argument, never a shell command line
        self.assertGreater(kw["timeout"], 2.2)


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeVM(BaseHTTPRequestHandler):
    posts, status = [], 204

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        FakeVM.posts.append((self.path, body))
        self.send_response(FakeVM.status)
        self.send_header("Content-Length", "0")
        self.end_headers()


def names(lines):
    return [l.split("{")[0] for l in lines]


def value_of(lines, name):
    for l in lines:
        if l.startswith(name + "{"):
            return l.split("} ")[1].split(" ")[0]
    return None


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.wt = w.Watcher("http://127.0.0.1:1", clock=self.clock)
        self.spec = w.validate_target(good(interval=15))
        self.wt.apply_targets([self.spec])

    def drain(self):
        lines, self.wt.pending = self.wt.pending, []
        return lines

    def test_a_success_writes_uptime_and_the_probe_series(self):
        self.wt.record(self.spec, w.Result(True, 0.003, "2 ms"))
        lines = self.drain()
        self.assertEqual(sorted(names(lines)), ["device_probe_duration_seconds", "device_probe_success",
                                                "device_report_interval_seconds", "device_uptime_seconds"])
        self.assertEqual(value_of(lines, "device_probe_success"), "1.0")
        self.assertEqual(value_of(lines, "device_report_interval_seconds"), "15.0")
        self.assertIn('device_id="router",job="watcher",kind="ping"', lines[0])
        self.assertTrue(lines[0].endswith(f" {int(self.clock.t * 1000)}"))

    def test_uptime_is_how_long_it_has_answered_continuously(self):
        self.wt.record(self.spec, w.Result(True, 0.01, "ok"))
        self.assertEqual(value_of(self.drain(), "device_uptime_seconds"), "0.0")
        self.clock.t += 15
        self.wt.record(self.spec, w.Result(True, 0.01, "ok"))
        self.assertEqual(value_of(self.drain(), "device_uptime_seconds"), "15.0")
        self.clock.t += 15
        self.wt.record(self.spec, w.Result(True, 0.01, "ok"))
        self.assertEqual(value_of(self.drain(), "device_uptime_seconds"), "30.0")

    def test_a_failure_writes_no_uptime_so_the_device_goes_quiet(self):
        self.wt.record(self.spec, w.Result(True, 0.01, "ok"))
        self.drain()
        self.clock.t += 15
        self.wt.record(self.spec, w.Result(False, 5.0, "timed out"))
        lines = self.drain()
        self.assertNotIn("device_uptime_seconds", names(lines))  # this is what makes the offline rule fire
        self.assertEqual(value_of(lines, "device_probe_success"), "0.0")
        self.assertEqual(value_of(lines, "device_probe_duration_seconds"), "5.0")
        self.assertEqual(value_of(lines, "device_report_interval_seconds"), "15.0")

    def test_coming_back_starts_the_uptime_over(self):
        self.wt.record(self.spec, w.Result(True, 0.01, "ok"))
        self.clock.t += 60
        self.wt.record(self.spec, w.Result(False, 5.0, "timed out"))
        self.clock.t += 15
        self.drain()
        self.wt.record(self.spec, w.Result(True, 0.01, "ok"))
        self.assertEqual(value_of(self.drain(), "device_uptime_seconds"), "0.0")

    def test_the_log_says_up_and_down_once_per_change(self):
        with Capture() as cap:
            for ok in [True, True, True, False, False, False, True, True]:
                self.wt.record(self.spec, w.Result(ok, 0.01, "detail"))
        self.assertEqual([m.split(" (")[0] for m in cap.messages], ["router: UP", "router: DOWN", "router: UP"])
        self.assertEqual(cap.records[1][0], "WARNING")

    def test_the_first_result_is_logged_even_when_it_is_a_failure(self):
        with Capture() as cap:
            self.wt.record(self.spec, w.Result(False, 1, "cannot resolve nas.lan"))
        self.assertEqual(cap.messages, ["router: DOWN (ping 192.168.1.1: cannot resolve nas.lan)"])

    def test_an_http_status_is_stored(self):
        spec = w.validate_target(good(id="web", kind="http", target="http://x/"))
        self.wt.apply_targets([spec])
        self.wt.record(spec, w.Result(False, 0.1, "HTTP 503", status=503))
        self.assertEqual(value_of(self.drain(), "device_http_status_code"), "503.0")

    def test_scraped_series_are_stored_with_our_labels(self):
        spec = w.validate_target(good(id="esp", kind="metrics", target="http://esp/metrics"))
        self.wt.apply_targets([spec])
        samples, stats = w.parse_metrics(ESPHOME)
        self.wt.record(spec, w.Result(True, 0.2, "ok", status=200, samples=samples, extra=stats))
        lines = self.drain()
        scraped = [l for l in lines if l.startswith("esphome_sensor_value{")]
        self.assertEqual(len(scraped), 2)
        self.assertIn('id="temperature"', scraped[0])
        self.assertIn('device_id="esp",job="watcher",kind="metrics"', scraped[0])
        self.assertEqual(value_of(lines, "device_scrape_series"), "4.0")
        self.assertIn("device_uptime_seconds", names(lines))

    def test_scraped_labels_cannot_overwrite_ours(self):
        spec = w.validate_target(good(id="esp", kind="metrics", target="http://esp/metrics"))
        self.wt.apply_targets([spec])
        samples, _ = w.parse_metrics('m{device_id="other",job="evil",kind="x",instance="i"} 1\n')
        self.wt.record(spec, w.Result(True, 0.2, "ok", samples=samples))
        line = [l for l in self.drain() if l.startswith("m{")][0]
        self.assertIn('exported_device_id="other"', line)
        self.assertIn('device_id="esp"', line)
        self.assertIn('job="watcher"', line)
        self.assertIn('exported_job="evil"', line)
        self.assertIn('instance="i"', line)

    def test_scrape_warnings_appear_once(self):
        spec = w.validate_target(good(id="esp", kind="metrics", target="http://esp/metrics"))
        self.wt.apply_targets([spec])
        with Capture() as cap:
            for _ in range(5):
                self.wt.record(spec, w.Result(True, 0.2, "ok", samples=[("a", {}, 1.0)], extra={"reserved": 2, "over_limit": 7}))
        warnings = [m for lvl, m in cap.records if lvl == "WARNING"]
        self.assertEqual(len(warnings), 2)
        self.assertTrue(any("reserved" in m for m in warnings))
        self.assertTrue(any("more than 2000 series" in m and "--max-series" in m for m in warnings))

    def test_nothing_is_written_for_a_target_that_was_removed(self):
        self.wt.apply_targets([])
        self.wt.record(self.spec, w.Result(True, 0.01, "ok"))
        self.assertEqual(self.wt.pending, [])
        self.assertEqual(self.wt.state, {})

    def test_the_buffer_is_bounded(self):
        for _ in range(w.MAX_BUFFER):
            self.wt.record(self.spec, w.Result(True, 0.01, "ok"))
        self.assertEqual(len(self.wt.pending), w.MAX_BUFFER)
        self.assertGreater(self.wt.stats["dropped_unsent"], 0)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.wt = w.Watcher("http://127.0.0.1:1", clock=Clock())

    def test_due_runs_a_new_target_at_once_then_at_its_interval(self):
        self.wt.apply_targets([w.validate_target(good(interval=15))])
        self.assertEqual(len(self.wt.due(1000.0)), 1)
        self.wt.inflight.clear()
        self.assertEqual(self.wt.due(1014.9), [])
        self.assertEqual(len(self.wt.due(1015.0)), 1)

    def test_a_slow_check_is_never_started_twice(self):
        self.wt.apply_targets([w.validate_target(good(interval=5))])
        self.assertEqual(len(self.wt.due(0.0)), 1)
        self.assertEqual(self.wt.due(100.0), [])  # still running
        self.wt.inflight.clear()
        self.assertEqual(len(self.wt.due(100.0)), 1)

    def test_changes_to_the_list_take_effect(self):
        a, b = w.validate_target(good(id="a")), w.validate_target(good(id="b", target="10.0.0.9"))
        self.wt.apply_targets([a, b])
        self.assertEqual({s["id"] for s in self.wt.due(0.0)}, {"a", "b"})
        with Capture() as cap:
            self.wt.apply_targets([a])
        self.assertEqual(set(self.wt.targets), {"a"})
        self.assertIn("b: no longer watched", cap.messages)
        self.wt.inflight.clear()
        changed = dict(a, interval=60)
        self.wt.state["a"]["since"] = 5.0
        self.wt.apply_targets([changed])
        self.assertIsNone(self.wt.state["a"]["since"])  # new settings, new streak
        self.assertEqual(len(self.wt.due(0.0)), 1)

    def test_run_check_survives_a_check_that_raises(self):
        spec = w.validate_target(good())
        self.wt.apply_targets([spec])
        self.wt.probes["ping"] = lambda s: 1 / 0
        with Capture() as cap:
            self.wt.run_check(spec)
        self.assertEqual(self.wt.state["router"]["up"], False)
        self.assertTrue(any("internal error" in m for m in cap.messages))
        self.assertNotIn("router", self.wt.inflight)


class DeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeVM)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeVM.posts, FakeVM.status = [], 204
        self.wt = w.Watcher(self.url, clock=Clock())
        self.spec = w.validate_target(good())
        self.wt.apply_targets([self.spec])

    def test_flush_posts_one_request_to_the_import_endpoint(self):
        self.wt.record(self.spec, w.Result(True, 0.01, "ok"))
        self.assertEqual(self.wt.flush(), 4)
        self.assertEqual(len(FakeVM.posts), 1)
        self.assertEqual(FakeVM.posts[0][0], "/api/v1/import/prometheus")
        self.assertEqual(self.wt.pending, [])
        self.assertEqual(self.wt.flush(), 0)

    def test_a_failed_post_keeps_the_results(self):
        self.wt.record(self.spec, w.Result(True, 0.01, "ok"))
        FakeVM.status = 500
        with self.assertRaises(OSError):
            self.wt.flush()
        self.assertEqual(len(self.wt.pending), 4)
        FakeVM.status = 204
        self.assertEqual(self.wt.flush(), 4)

    def test_an_outage_is_logged_once_and_so_is_the_recovery(self):
        self.wt.record(self.spec, w.Result(True, 0.01, "ok"))
        FakeVM.status = 500
        with Capture() as cap:
            for _ in range(4):
                self.wt.flush_logged()
            FakeVM.status = 204
            self.wt.flush_logged()
            self.wt.flush_logged()
        self.assertEqual(len(cap.records), 2)
        self.assertIn("cannot reach VictoriaMetrics", cap.messages[0])
        self.assertIn("reachable again", cap.messages[1])


class ServiceTests(unittest.TestCase):
    def test_config_from_environment_and_flags(self):
        c = w.parse_config([], env={})
        self.assertEqual((c.targets, c.vm_url, c.once), ("/watch/targets.json", "http://victoriametrics:8428", False))
        c = w.parse_config(["--targets", "/t.json", "--vm-url", "http://vm:1", "--once"], env={"TARGETS_FILE": "/ignored"})
        self.assertEqual((c.targets, c.vm_url, c.once), ("/t.json", "http://vm:1", True))
        c = w.parse_config([], env={"TARGETS_FILE": "/e.json", "VM_URL": "http://e:2"})
        self.assertEqual((c.targets, c.vm_url), ("/e.json", "http://e:2"))

    def test_help_runs_on_a_bare_python(self):
        r = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "watcher", "watcher.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--once", r.stdout)

    def test_once_checks_everything_and_delivers(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        vm = HTTPServer(("127.0.0.1", 0), FakeVM)
        threading.Thread(target=vm.serve_forever, daemon=True).start()
        FakeVM.posts, FakeVM.status = [], 204
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), vm.shutdown(), vm.server_close()))
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "targets.json")
            with open(path, "w") as f:
                json.dump({"targets": [good(id="web", kind="http", target=base + "/ok"),
                                       good(id="esp", kind="metrics", target=base + "/metrics"),
                                       good(id="gone", kind="tcp", target="127.0.0.1:1")]}, f)
            cfg = w.parse_config(["--targets", path, "--vm-url", f"http://127.0.0.1:{vm.server_address[1]}", "--once"], env={})
            w.run(cfg)
        body = FakeVM.posts[0][1]
        self.assertIn('device_uptime_seconds{device_id="web"', body)
        self.assertIn('esphome_sensor_value{id="temperature"', body)
        self.assertIn('device_probe_success{device_id="gone",job="watcher",kind="tcp"} 0.0', body)
        self.assertNotIn('device_uptime_seconds{device_id="gone"', body)


class FileTests(unittest.TestCase):
    def test_the_service_has_no_third_party_imports(self):
        # It runs in the stock python:alpine image with nothing installed.
        import ast
        with open(os.path.join(ROOT, "watcher", "watcher.py")) as f:
            tree = ast.parse(f.read())
        stdlib = set(sys.stdlib_module_names)
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module.split(".")[0])
        self.assertEqual(found - stdlib, set())

    def test_no_windows_line_endings(self):
        with open(os.path.join(ROOT, "watcher", "watcher.py"), "rb") as f:
            self.assertNotIn(b"\r", f.read())


if __name__ == "__main__":
    unittest.main()
