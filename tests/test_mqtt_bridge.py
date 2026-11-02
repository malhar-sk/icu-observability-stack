"""Tests for bridge/mqtt_bridge.py. Stdlib only: no broker and no paho are needed. A fake VictoriaMetrics HTTP
server records what the bridge posts. The live path (a real Mosquitto, a real VictoriaMetrics, the dashboard)
is covered by scripts/validate_mqtt.sh and described in docs/VALIDATION.md.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import io
import logging
import os
import re
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("mqtt_bridge", os.path.join(ROOT, "bridge", "mqtt_bridge.py"))
mb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mb)
mb.log.addHandler(logging.NullHandler())  # keeps the bridge's warnings out of the test output


class Capture(logging.Handler):
    """Collects log records; works on every Python version this project supports (assertNoLogs needs 3.10)."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())

    def __enter__(self):
        mb.log.addHandler(self)
        return self

    def __exit__(self, *exc):
        mb.log.removeHandler(self)


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


class ParseTests(unittest.TestCase):
    def test_topics(self):
        self.assertEqual(mb.parse_topic("fleet/garage-esp32/temperature_celsius"), ("garage-esp32", "temperature_celsius"))
        self.assertEqual(mb.parse_topic("fleet/a.b_c-1/x"), ("a.b_c-1", "x"))
        for bad in ["", "fleet", "fleet/a", "fleet/a/b/c", "other/a/b", "fleet//b", "fleet/a/", "Fleet/a/b",
                    "fleet/UPPER/b", "fleet/-lead/b", "fleet/a b/b", "fleet/a/Bad", "fleet/a/1x", "fleet/a/has-dash",
                    "fleet/" + "a" * 64 + "/b", "fleet/a/" + "b" * 64, 'fleet/a"x/b', "fleet/a/b\n", "fleet/a\nb/c",
                    "fleet/+/b", "fleet/a/#", "$SYS/broker/uptime"]:
            self.assertIsNone(mb.parse_topic(bad), repr(bad))

    def test_device_id_rule_is_the_agents_rule(self):
        sys.path.insert(0, os.path.join(ROOT, "agent"))
        from device_agent import DEVICE_ID_RE
        self.assertEqual(mb.DEVICE_ID_RE.pattern, DEVICE_ID_RE.pattern)

    def test_values(self):
        for text, want in [(b"21.5", 21.5), (b"0", 0.0), (b"-3", -3.0), (b"+4.25", 4.25), (b" 7 ", 7.0), (b"7\n", 7.0),
                           (b"1e3", 1000.0), (b"2.5E-2", 0.025), (b".5", 0.5), (b"5.", 5.0)]:
            self.assertEqual(mb.parse_value(text), want, text)
        for bad in [b"", b"abc", b"online", b"offline", b"nan", b"NaN", b"inf", b"-inf", b"1e999", b"1,5", b"1 2",
                    b"0x10", b"true", b'"5"', b"{\"v\":5}", b"5;DROP", b"1\n2", b"\xff\xfe", b"9" * 65, b"--1"]:
            self.assertIsNone(mb.parse_value(bad), bad)


class HandleTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.b = mb.Bridge("http://127.0.0.1:1", clock=self.clock)
        self.stamp = int(self.clock.t * 1000)

    def test_a_reading_becomes_a_device_metric_stamped_on_arrival(self):
        self.b.handle("fleet/garage/temperature_celsius", b"21.5")
        self.assertEqual(self.b.pending, [f'device_temperature_celsius{{device_id="garage",job="mqtt-bridge"}} 21.5 {self.stamp}'])

    def test_every_line_is_valid_prometheus_text(self):
        for t, p in [("fleet/a/uptime_seconds", b"12"), ("fleet/a/x_percent", b"1e-3"), ("fleet/b.c_d-1/y", b"-4")]:
            self.b.handle(t, p)
        for line in self.b.pending:
            self.assertRegex(line, r'^device_[a-z][a-z0-9_]*\{device_id="[a-z0-9][a-z0-9._-]*",job="mqtt-bridge"\} -?[0-9.e+-]+ \d{13}$')

    def test_bad_messages_are_dropped_and_counted(self):
        self.b.handle("fleet/a/b/c", b"1")
        self.b.handle("fleet/a/temp", b"hot")
        self.b.handle("fleet/a/report_interval_seconds", b"0")
        self.b.handle("fleet/a/report_interval_seconds", b"999999")
        self.assertEqual(self.b.pending, [])
        self.assertEqual((self.b.stats["invalid_topic"], self.b.stats["invalid_value"]), (1, 3))

    def test_nothing_in_a_payload_can_inject_a_line(self):
        for p in [b"1\nevil_metric 1", b"1 1\nx 2", b'1"}\ndevice_x{a="b"} 1']:
            self.b.handle("fleet/a/v", p)
        self.assertEqual(self.b.pending, [])

    def test_retained_readings_are_never_treated_as_fresh(self):
        self.b.handle("fleet/dead/uptime_seconds", b"500", retained=True)
        self.b.handle("fleet/dead/temperature_celsius", b"20", retained=True)
        self.assertEqual(self.b.pending, [])
        self.assertEqual(self.b.stats["retained_ignored"], 2)
        self.assertNotIn("dead", self.b.first_seen)  # and it does not count as a device we have seen

    def test_a_retained_interval_is_remembered_but_does_not_make_a_device_appear(self):
        self.b.handle("fleet/sleepy/report_interval_seconds", b"300", retained=True)
        self.assertEqual(self.b.pending, [])
        self.assertEqual(self.b.interval, {"sleepy": 300.0})
        self.assertNotIn("sleepy", self.b.first_seen)
        self.clock.t += 100
        self.b.handle("fleet/sleepy/uptime_seconds", b"42")  # its first real report
        self.assertEqual([l.split("{")[0] for l in self.b.pending], ["device_uptime_seconds", "device_report_interval_seconds"])
        self.assertTrue(self.b.pending[1].split("} ")[1].startswith("300.0 "))

    def test_the_interval_travels_with_every_uptime_sample(self):
        self.b.handle("fleet/d/report_interval_seconds", b"60")
        self.assertEqual(self.b.pending, [])  # config alone is not a heartbeat
        for n in range(3):
            self.clock.t += 60
            self.b.handle("fleet/d/uptime_seconds", str(n).encode())
        names = [l.split("{")[0] for l in self.b.pending]
        self.assertEqual(names, ["device_uptime_seconds", "device_report_interval_seconds"] * 3)

    def test_no_interval_declared_means_no_interval_series(self):
        self.b.handle("fleet/d/uptime_seconds", b"1")
        self.assertEqual(len(self.b.pending), 1)

    def test_a_device_can_change_its_interval(self):
        self.b.handle("fleet/d/report_interval_seconds", b"60")
        self.b.handle("fleet/d/report_interval_seconds", b"600")
        self.b.handle("fleet/d/uptime_seconds", b"1")
        self.assertIn(" 600.0 ", self.b.pending[-1])

    def test_limits_protect_an_open_broker(self):
        for i in range(mb.MAX_DEVICES + 10):
            self.b.handle(f"fleet/dev{i}/uptime_seconds", b"1")
        self.assertEqual(len(self.b.metrics), mb.MAX_DEVICES)
        self.assertEqual(self.b.stats["limit"], 10)
        for i in range(mb.MAX_METRICS_PER_DEVICE + 5):
            self.b.handle(f"fleet/dev0/m{i}", b"1")
        self.assertEqual(len(self.b.metrics["dev0"]), mb.MAX_METRICS_PER_DEVICE)
        for i in range(mb.MAX_DEVICES + 10):  # retained config alone cannot grow without bound either
            self.b.handle(f"fleet/cfg{i}/report_interval_seconds", b"60", retained=True)
        self.assertLessEqual(len(self.b.interval), mb.MAX_DEVICES)

    def test_warnings_are_logged_once(self):
        with self.assertLogs("mqtt-bridge", level="WARNING") as cm:
            for _ in range(50):
                self.b.handle("fleet/a/temp", b"hot")
                self.b.handle("nonsense", b"1")
        self.assertEqual(len(cm.output), 2)

    def test_a_device_with_readings_but_no_uptime_is_called_out_once(self):
        self.b.handle("fleet/mute/temperature_celsius", b"20")
        self.b.handle("fleet/fine/uptime_seconds", b"1")
        self.b.handle("fleet/fine/temperature_celsius", b"20")
        self.clock.t += mb.NO_UPTIME_GRACE - 1
        with Capture() as early:
            self.b.check_uptime()
        self.assertEqual(early.messages, [])  # still within the grace period
        self.clock.t += 2
        with Capture() as late:
            self.b.check_uptime()
            self.b.check_uptime()
        self.assertEqual(len(late.messages), 1)
        self.assertIn("mute", late.messages[0])
        self.assertIn("fleet/mute/uptime_seconds", late.messages[0])
        self.assertNotIn("fine", late.messages[0])


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
        self.b = mb.Bridge(self.url, clock=Clock())

    def test_flush_posts_all_lines_in_one_request_to_the_import_endpoint(self):
        self.b.handle("fleet/a/uptime_seconds", b"1")
        self.b.handle("fleet/a/temperature_celsius", b"2")
        self.assertEqual(self.b.flush(), 2)
        self.assertEqual(len(FakeVM.posts), 1)
        path, body = FakeVM.posts[0]
        self.assertEqual(path, "/api/v1/import/prometheus")
        self.assertEqual(len(body.strip().splitlines()), 2)
        self.assertEqual(self.b.pending, [])
        self.assertEqual(self.b.flush(), 0)  # nothing pending: no request
        self.assertEqual(len(FakeVM.posts), 1)

    def test_a_failed_post_keeps_the_readings_for_the_next_try(self):
        self.b.handle("fleet/a/uptime_seconds", b"1")
        FakeVM.status = 500
        with self.assertRaises(OSError):
            self.b.flush()
        self.assertEqual(len(self.b.pending), 1)
        self.b.handle("fleet/a/uptime_seconds", b"2")
        FakeVM.status = 204
        self.assertEqual(self.b.flush(), 2)
        self.assertEqual(self.b.pending, [])
        self.assertEqual(FakeVM.posts[-1][1].count("\n"), 2)

    def test_the_buffer_is_bounded_and_the_oldest_lines_go_first(self):
        b = mb.Bridge("http://127.0.0.1:1", clock=Clock())  # nothing listens here
        for i in range(mb.MAX_BUFFER + 5):
            b.pending.append(f"line{i}")
        with self.assertRaises(OSError):
            b.flush()
        self.assertEqual(len(b.pending), mb.MAX_BUFFER)
        self.assertEqual(b.pending[0], "line5")
        self.assertEqual(b.stats["dropped_unsent"], 5)

    def test_flush_logged_reports_an_outage_once_and_the_recovery_once(self):
        self.b.handle("fleet/a/uptime_seconds", b"1")
        FakeVM.status = 500
        with self.assertLogs("mqtt-bridge", level="INFO") as cm:
            for _ in range(5):
                self.b.flush_logged()
            FakeVM.status = 204
            self.b.flush_logged()
            self.b.flush_logged()
        self.assertEqual(len(cm.output), 2)
        self.assertIn("cannot reach VictoriaMetrics", cm.output[0])
        self.assertIn("reachable again", cm.output[1])

    def test_local_requests_ignore_proxy_settings(self):
        old = {k: os.environ.get(k) for k in ("HTTP_PROXY", "http_proxy")}
        os.environ["HTTP_PROXY"] = os.environ["http_proxy"] = "http://127.0.0.1:9"
        try:
            b = mb.Bridge(self.url, clock=Clock())
            b.handle("fleet/a/uptime_seconds", b"1")
            self.assertEqual(b.flush(), 1)
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


class ConfigTests(unittest.TestCase):
    def test_env_and_flags(self):
        c = mb.parse_config([], env={"MQTT_HOST": "broker.lan", "MQTT_PASSWORD": "pw", "MQTT_USERNAME": "fleet"})
        self.assertEqual((c.host, c.port, c.username, c.password, c.vm_url),
                         ("broker.lan", 1883, "fleet", "pw", "http://victoriametrics:8428"))
        c = mb.parse_config(["--host", "x", "--port", "8883", "--username", "u", "--vm-url", "http://vm:1"],
                            env={"MQTT_HOST": "ignored", "MQTT_USERNAME": "ignored"})
        self.assertEqual((c.host, c.port, c.username, c.vm_url), ("x", 8883, "u", "http://vm:1"))

    def test_the_password_has_no_flag(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            mb.parse_config(["--host", "x", "--password", "secret"], env={})

    def test_host_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            mb.parse_config([], env={})

    def test_empty_env_values_count_as_unset(self):
        c = mb.parse_config(["--host", "x"], env={"MQTT_PORT": "", "MQTT_USERNAME": "", "MQTT_PASSWORD": ""})
        self.assertEqual((c.port, c.username, c.password), (1883, None, None))


class FileTests(unittest.TestCase):
    def test_help_works_without_paho(self):
        import subprocess
        r = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "bridge", "mqtt_bridge.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--host", r.stdout)

    def test_the_password_is_never_logged(self):
        with open(os.path.join(ROOT, "bridge", "mqtt_bridge.py")) as f:
            source = f.read()
        for m in re.finditer(r"log\.\w+\((.*)", source):
            self.assertNotIn("password", m.group(1).lower(), m.group(0))


if __name__ == "__main__":
    unittest.main()
