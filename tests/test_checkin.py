"""Tests for watcher/checkin.py. Stdlib only. The HTTP tests run the real server on localhost; a fake VictoriaMetrics
records what is delivered. The live path (real containers, real curl, the real dashboard) is covered by
scripts/validate_checkin.sh.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import contextlib
import http.client
import importlib.util
import io
import logging
import os
import sys
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("checkin", os.path.join(ROOT, "watcher", "checkin.py"))
ci = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci)
ci.log.addHandler(logging.NullHandler())
ci.core.log.addHandler(logging.NullHandler())

KEY = "s3cret-key"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeVM(BaseHTTPRequestHandler):
    posts = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        FakeVM.posts.append(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


def names(lines):
    return [l.split("{")[0] for l in lines]


def value_of(lines, name):
    for l in lines:
        if l.startswith(name + "{"):
            return l.split("} ")[1].split(" ")[0]


class HandleTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.c = ci.CheckIns("http://127.0.0.1:1", KEY, clock=self.clock)

    def hit(self, device_id="job-1", key=KEY, **params):
        return self.c.handle(device_id, {k: [str(v)] for k, v in params.items()}, key)

    def drain(self):
        lines, self.c.pending = self.c.pending, []
        return lines

    def test_a_check_in_writes_uptime_and_the_interval(self):
        self.assertEqual(self.hit(every=60), (200, "ok"))
        lines = self.drain()
        self.assertEqual(sorted(names(lines)), ["device_report_interval_seconds", "device_uptime_seconds"])
        self.assertIn('device_id="job-1",job="checkin"', lines[0])
        self.assertTrue(lines[0].endswith(f" {int(self.clock.t * 1000)}"))
        self.assertEqual(value_of(lines, "device_report_interval_seconds"), "60.0")

    def test_without_every_no_interval_is_written(self):
        self.hit()
        self.assertEqual(names(self.drain()), ["device_uptime_seconds"])

    def test_readings_become_device_metrics(self):
        self.hit(temperature_celsius="21.5", battery_volts="-3", big="1e3")
        lines = self.drain()
        self.assertEqual(sorted(names(lines)), ["device_battery_volts", "device_big", "device_temperature_celsius", "device_uptime_seconds"])
        self.assertEqual(value_of(lines, "device_temperature_celsius"), "21.5")
        self.assertEqual(value_of(lines, "device_big"), "1000.0")

    def test_the_key_is_checked_and_nothing_is_stored_without_it(self):
        for key in ["", "wrong", KEY + "x", KEY[:-1], None]:
            self.assertEqual(self.c.handle("job-1", {}, key)[0], 401, key)
        self.assertEqual(self.c.pending, [])
        self.assertEqual(ci.CheckIns("http://x", "", clock=self.clock).handle("a", {}, "")[0], 401)  # no key configured: nobody gets in
        self.assertEqual(self.c.stats["refused"], 5)

    def test_bad_input_is_refused_with_a_reason_and_stores_nothing(self):
        for device_id, params, text in [("Bad ID", {}, "device id"), ("-x", {}, "device id"), ("a" * 64, {}, "device id"),
                                        ("ok", {"every": "4"}, "from 5 to 28800"), ("ok", {"every": "28801"}, "from 5 to 28800"),
                                        ("ok", {"every": "soon"}, "number of seconds"), ("ok", {"every": "nan"}, "number of seconds"),
                                        ("ok", {"every": ""}, "number of seconds"),
                                        ("ok", {"Temp": "1"}, "not a reading name"), ("ok", {"1x": "1"}, "not a reading name"),
                                        ("ok", {"has-dash": "1"}, "not a reading name"), ("ok", {"t": "hot"}, "plain number"),
                                        ("ok", {"t": "nan"}, "plain number"), ("ok", {"t": "inf"}, "plain number"),
                                        ("ok", {"t": "1;2"}, "plain number"), ("ok", {"t": ""}, "plain number"),
                                        ("ok", {"report_interval_seconds": "60"}, "every=")]:
            status, message = self.c.handle(device_id, {k: [v] for k, v in params.items()}, KEY)
            self.assertEqual(status, 400, (device_id, params))
            self.assertIn(text, message, (device_id, params))
        self.assertEqual(self.c.handle("ok", {"t": ["1", "2"]}, KEY)[0], 400)  # given twice
        self.assertEqual(self.c.handle("ok", {f"m{i}": ["1"] for i in range(ci.MAX_PARAMS + 1)}, KEY)[0], 400)
        self.assertEqual(self.c.pending, [])
        self.assertEqual(self.c.devices, {})

    def test_a_reading_cannot_inject_a_line(self):
        for bad in ["1\nevil 1", "1 1\nx 2", '1"}\nx{a="b"} 1']:
            self.assertEqual(self.hit(t=bad)[0], 400)
        self.assertEqual(self.c.pending, [])

    def test_uptime_counts_from_the_start_of_the_streak(self):
        self.hit(every=60)
        self.assertEqual(value_of(self.drain(), "device_uptime_seconds"), "0.0")
        for expect in ("60.0", "120.0", "180.0"):
            self.clock.t += 60
            self.hit(every=60)
            self.assertEqual(value_of(self.drain(), "device_uptime_seconds"), expect)

    def test_a_gap_longer_than_the_limit_starts_a_new_streak(self):
        self.hit(every=60)
        self.clock.t += 180  # exactly 3 x every: still inside the limit
        self.hit(every=60)
        self.assertEqual(value_of(self.drain()[-2:], "device_uptime_seconds"), "180.0")
        self.clock.t += 181
        self.hit(every=60)
        self.assertEqual(value_of(self.drain(), "device_uptime_seconds"), "0.0")

    def test_the_30_second_floor_applies_when_nothing_is_declared(self):
        self.hit()
        self.clock.t += 30
        self.hit()
        self.assertEqual(value_of(self.drain()[-1:], "device_uptime_seconds"), "30.0")
        self.clock.t += 31
        self.hit()
        self.assertEqual(value_of(self.drain(), "device_uptime_seconds"), "0.0")

    def test_a_declared_interval_is_remembered_for_the_next_check_in(self):
        self.hit(every=3600)
        self.clock.t += 3000
        self.hit()  # no every this time
        lines = self.drain()
        self.assertEqual(value_of(lines[-2:], "device_report_interval_seconds"), "3600.0")
        self.assertEqual(value_of(lines[-2:], "device_uptime_seconds"), "3000.0")  # 3000 s is inside 3 x 3600

    def test_the_device_can_send_its_own_uptime(self):
        self.hit(uptime_seconds=86400, t=1)
        lines = self.drain()
        self.assertEqual(value_of(lines, "device_uptime_seconds"), "86400.0")
        self.assertEqual(names(lines).count("device_uptime_seconds"), 1)

    def test_devices_are_independent(self):
        self.hit("a", every=60)
        self.clock.t += 100
        self.hit("b", every=60)
        self.assertEqual(value_of(self.drain()[-2:], "device_uptime_seconds"), "0.0")
        self.hit("a", every=60)
        self.assertEqual(value_of(self.drain(), "device_uptime_seconds"), "100.0")

    def test_the_number_of_devices_is_limited(self):
        for i in range(ci.MAX_DEVICES):
            self.assertEqual(self.hit(f"d{i}")[0], 200)
        self.assertEqual(self.hit("one-too-many")[0], 503)
        self.assertEqual(self.hit("d0")[0], 200)  # a known device is still welcome


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vm = HTTPServer(("127.0.0.1", 0), FakeVM)
        threading.Thread(target=cls.vm.serve_forever, daemon=True).start()
        cls.checkins = ci.CheckIns(f"http://127.0.0.1:{cls.vm.server_address[1]}", KEY)
        cls.server = ci.Server(("127.0.0.1", 0), cls.checkins)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        for srv in (cls.server, cls.vm):
            srv.shutdown()
            srv.server_close()

    def setUp(self):
        FakeVM.posts = []
        self.checkins.pending, self.checkins.devices = [], {}

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body)
            r = conn.getresponse()
            return r.status, r.read().decode()
        finally:
            conn.close()

    def test_get_and_post_both_work(self):
        for method in ("GET", "POST"):
            self.assertEqual(self.request(method, f"/checkin/web-1?key={KEY}&every=60&temperature_celsius=21.5", body=b"ignored"),
                             (200, "ok\n"), method)
        self.assertEqual(self.request("HEAD", f"/checkin/web-1?key={KEY}")[0], 200)

    def test_the_check_in_is_delivered_to_victoriametrics(self):
        self.request("GET", f"/checkin/web-2?key={KEY}&every=60&temperature_celsius=21.5")
        self.assertEqual(self.checkins.flush(), 3)
        body = FakeVM.posts[0]
        self.assertIn('device_uptime_seconds{device_id="web-2",job="checkin"}', body)
        self.assertIn('device_temperature_celsius{device_id="web-2",job="checkin"} 21.5', body)

    def test_error_statuses(self):
        self.assertEqual(self.request("GET", "/checkin/web-1?key=nope")[0], 401)
        self.assertEqual(self.request("GET", "/checkin/web-1")[0], 401)
        status, text = self.request("GET", f"/checkin/web-1?key={KEY}&every=1")
        self.assertEqual(status, 400)
        self.assertIn("from 5 to 28800", text)
        self.assertEqual(self.request("GET", f"/checkin/Web%201?key={KEY}")[0], 400)  # an encoded space is not a legal id
        self.assertEqual(self.request("GET", f"/checkin/?key={KEY}")[0], 404)
        self.assertEqual(self.request("GET", f"/checkin/a/b?key={KEY}")[0], 404)
        self.assertEqual(self.request("GET", "/")[0], 404)
        self.assertEqual(self.request("GET", f"/checkin/../health?key={KEY}")[0], 404)
        self.assertEqual(self.request("GET", f"/checkin/x?key={KEY}&" + "a=1&" * 100)[0], 400)
        self.assertEqual(self.request("GET", "/checkin/x?key=" + "k" * 3000)[0], 414)
        self.assertEqual(self.checkins.pending, [])

    def test_health_needs_no_key_and_says_nothing_else(self):
        self.assertEqual(self.request("GET", "/health"), (200, "ok\n"))

    def test_the_404_explains_the_address(self):
        self.assertIn("/checkin/<device_id>", self.request("GET", "/nope")[1])

    def test_the_key_never_reaches_a_log(self):
        import contextlib
        stderr, records = io.StringIO(), []

        class Grab(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
        grab = Grab()
        ci.core.log.addHandler(grab)
        ci.core.log.setLevel(logging.DEBUG)
        try:
            with contextlib.redirect_stderr(stderr):
                self.request("GET", f"/checkin/web-3?key={KEY}&every=60")
                self.request("GET", f"/checkin/web-3?key={KEY}&every=1")        # a refused one is logged
                self.request("GET", "/checkin/web-4?key=wrong-" + KEY)          # so is a wrong key
        finally:
            ci.core.log.removeHandler(grab)
        everything = stderr.getvalue() + "\n".join(records)
        self.assertNotIn(KEY, everything)
        self.assertTrue(any("web-3: refused (400)" in r for r in records))
        self.assertTrue(any("web-4: refused (401)" in r for r in records))

    def test_a_refusal_is_logged_once_per_device_and_status(self):
        records = []

        class Grab(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
        grab = Grab()
        ci.core.log.addHandler(grab)
        ci.core.log.setLevel(logging.INFO)
        try:
            for _ in range(5):
                self.request("GET", "/checkin/loud?key=wrong")
        finally:
            ci.core.log.removeHandler(grab)
        self.assertEqual(len([r for r in records if "loud" in r]), 1)


class ConfigTests(unittest.TestCase):
    def test_a_key_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            ci.parse_config([], env={})

    def test_defaults_and_flags(self):
        c = ci.parse_config([], env={"CHECKIN_KEY": "k"})
        self.assertEqual((c.port, c.key, c.vm_url), (8081, "k", "http://victoriametrics:8428"))
        c = ci.parse_config(["--port", "9000", "--vm-url", "http://vm:1"], env={"CHECKIN_KEY": "k", "VM_URL": "ignored"})
        self.assertEqual((c.port, c.vm_url), (9000, "http://vm:1"))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            ci.parse_config(["--port", "0"], env={"CHECKIN_KEY": "k"})

    def test_the_key_has_no_command_line_flag(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            ci.parse_config(["--key", "x"], env={"CHECKIN_KEY": "k"})


class FileTests(unittest.TestCase):
    def test_only_the_standard_library_is_used(self):
        import ast
        with open(os.path.join(ROOT, "watcher", "checkin.py")) as f:
            tree = ast.parse(f.read())
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module.split(".")[0])
        self.assertEqual(found - set(sys.stdlib_module_names), set())

    def test_the_request_line_is_never_logged(self):
        # the default BaseHTTPRequestHandler.log_message prints the whole request line (with the key): ours must not
        self.assertNotEqual(ci.Handler.log_message, ci.http.server.BaseHTTPRequestHandler.log_message)

    def test_the_interval_cap_keeps_the_limit_inside_the_dashboards_lookback(self):
        self.assertLessEqual(3 * ci.MAX_EVERY, 86400)


if __name__ == "__main__":
    unittest.main()
