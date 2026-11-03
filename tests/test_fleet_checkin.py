"""Tests for `fleet add --checkin` and the other places the check-in service touches the CLI. Stdlib only. No Docker:
subprocess calls are patched, and a small local web server stands in for the service's /health. The live path (real
container, real curl) is covered by scripts/validate_checkin.sh.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import contextlib
import io
import os
import re
import stat
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_fleet import FakeVM, WithFakeVM, dev, fleet, run  # noqa: E402
from test_fleet_mqtt import WithEnv  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Health(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200 if self.path == "/health" else 404)
        self.send_header("Content-Length", "3")
        self.end_headers()
        self.wfile.write(b"ok\n")


class WithCheckin(WithEnv, WithFakeVM):
    """A fake VictoriaMetrics, a private .env, and a local stand-in for the check-in service's /health."""

    def setUp(self):
        WithFakeVM.setUp(self)
        WithEnv.setUp(self)
        self.srv = HTTPServer(("127.0.0.1", 0), Health)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.srv.shutdown(), self.srv.server_close()))
        self.port = self.srv.server_address[1]
        self.write(f"CHECKIN_PORT={self.port}\n")
        for name, value in [("require_docker", lambda: None), ("compose_ps", lambda: {"checkin": "running"}),
                            ("lan_ip", lambda: "10.0.0.5")]:
            p = mock.patch.object(fleet, name, value)
            p.start()
            self.addCleanup(p.stop)

    def add(self, *argv):
        return run(["add", *argv])


class CronTests(unittest.TestCase):
    def test_what_cron_can_say(self):
        for every, want in [(60, "* * * * *"), (300, "*/5 * * * *"), (120, "*/2 * * * *"), (900, "*/15 * * * *"),
                            (3600, "0 * * * *"), (7200, "0 */2 * * *"), (28800, "0 */8 * * *")]:
            self.assertEqual(fleet.cron_line(every), want, every)
        for every in [5, 90, 45, 420, 5400, 10800 + 1, 18000, 25200]:  # 5 h and 7 h do not divide a day; 7 min does not divide an hour
            self.assertIsNone(fleet.cron_line(every), every)


class AddCheckinTests(WithCheckin):
    def test_the_first_use_creates_a_key_turns_the_service_on_and_prints_a_command_that_works(self):
        code, out, _ = self.add("backup-job", "--checkin", "--no-wait")
        self.assertEqual(code, 0)
        env = fleet.read_env()
        self.assertRegex(env["CHECKIN_KEY"], r"^[0-9a-f]{24}$")
        self.assertEqual(env["COMPOSE_PROFILES"], "checkin")
        self.assertEqual(stat.S_IMODE(os.stat(self.env_path).st_mode), 0o600)
        self.assertIn(["docker", "compose", "up", "-d", "checkin"], self.calls)
        url = f"http://10.0.0.5:{self.port}/checkin/backup-job?key={env['CHECKIN_KEY']}&every=60"
        self.assertIn(f'curl -fsS -m 10 "{url}"', out)
        self.assertIn(f'* * * * * curl -fsS -m 10 "{url}" >/dev/null', out)
        self.assertIn("OFFLINE after 3m 00s", out)
        self.assertIn("END of your script", out)

    def test_the_key_is_kept_so_earlier_commands_stay_valid(self):
        self.add("a", "--checkin", "--no-wait")
        first = fleet.read_env()["CHECKIN_KEY"]
        self.add("b", "--checkin", "--no-wait")
        self.assertEqual(fleet.read_env()["CHECKIN_KEY"], first)

    def test_every_is_used_and_checked(self):
        _, out, _ = self.add("slow", "--checkin", "--every", "3600", "--no-wait")
        self.assertIn("&every=3600", out)
        self.assertIn("0 * * * * curl", out)
        _, out, _ = self.add("odd", "--checkin", "--every", "90", "--no-wait")
        self.assertNotIn("crontab", out)  # cron cannot say every 90 s: no made-up schedule
        for bad in ("4", "28801", "0", "-5"):
            code, _, err = self.add("x", "--checkin", "--every", bad, "--no-wait")
            self.assertEqual(code, 1, bad)
            self.assertIn("5 to 28800", err)

    def test_other_profiles_survive(self):
        self.write(f"COMPOSE_PROFILES=mqtt,watch\nMQTT_PASSWORD=x\nCHECKIN_PORT={self.port}\n")
        self.add("a", "--checkin", "--no-wait")
        self.assertEqual(fleet.read_env()["COMPOSE_PROFILES"], "mqtt,watch,checkin")
        self.assertEqual(fleet.mqtt_mode(), "bundled")
        self.assertTrue(fleet.watch_enabled() and fleet.checkin_enabled())

    def test_the_host_option_is_used(self):
        _, out, _ = self.add("a", "--checkin", "--host", "pi.lan", "--no-wait")
        self.assertIn(f"http://pi.lan:{self.port}/checkin/a?key=", out)
        self.assertNotIn("can change", out)  # the hotspot hint only appears for an auto-detected address

    def test_an_id_that_is_reporting_in_another_way_is_refused(self):
        self.devices(taken=dev(age=3))
        code, _, err = self.add("taken", "--checkin", "--no-wait")
        self.assertEqual(code, 1)
        self.assertIn("already reporting", err)

    def test_a_busy_port_is_explained_and_changes_nothing(self):
        before = self.read()
        with mock.patch.object(fleet, "compose_ps", lambda: {}):  # our service is not running, but something listens there
            code, _, err = self.add("a", "--checkin", "--no-wait")
        self.assertEqual(code, 1)
        self.assertIn("already in use", err)
        self.assertIn("CHECKIN_PORT=8090", err)
        self.assertEqual(self.read(), before)
        self.assertEqual(self.calls, [])

    def test_the_port_comes_from_env_and_a_bad_value_falls_back(self):
        self.assertEqual(fleet.checkin_port({"CHECKIN_PORT": "8090"}), 8090)
        for bad in ("", "abc", "0", "70000", "-1"):
            self.assertEqual(fleet.checkin_port({"CHECKIN_PORT": bad}), 8081, bad)
        self.assertEqual(fleet.checkin_port({}), 8081)

    def test_a_service_that_never_answers_is_reported(self):
        self.write("CHECKIN_PORT=1\n")  # nothing listens on port 1
        with mock.patch.object(fleet.time, "sleep"):
            code, _, err = self.add("a", "--checkin", "--no-wait")
        self.assertEqual(code, 1)
        self.assertIn("did not answer", err)

    def test_it_waits_for_the_first_check_in(self):
        FakeVM.state["hide_until_query"] = 3
        self.devices(**{"backup-job": dev(age=2, job="checkin", cpu=None, mem=None, temp=None)})
        code, out, _ = self.add("backup-job", "--checkin", "--timeout", "5")
        self.assertEqual(code, 0)
        self.assertIn("is online", out)
        self.assertIn("(via checkin)", out)

    def test_it_gives_up_with_a_useful_hint(self):
        code, out, _ = self.add("ghost", "--checkin", "--timeout", "1")
        self.assertEqual(code, 1)
        self.assertIn("Run the curl above by hand", out)

    def test_the_two_ways_exclude_each_other(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            fleet.build_parser().parse_args(["add", "x", "--mqtt", "--checkin"])

    def test_the_agent_instructions_are_unchanged_without_the_flag(self):
        _, out, _ = run(["add", "kitchen-pi", "--host", "10.0.0.5", "--no-wait"])
        self.assertIn("--device-id kitchen-pi --endpoint 10.0.0.5:4317", out)
        self.assertNotIn("checkin", out)


class StackTests(WithCheckin):
    def test_up_restarts_a_running_checkin_service_and_a_running_watcher(self):
        self.write(f"COMPOSE_PROFILES=watch,checkin\nCHECKIN_KEY=k\nCHECKIN_PORT={self.port}\n")
        patches = [mock.patch.object(fleet, n, v) for n, v in [
            ("missing_images", lambda: []), ("service_checks", lambda: {"victoriametrics": (True, "healthy")}),
            ("dashboard_state", lambda: "ok"), ("fetch_devices", lambda: {}), ("notify_configured", lambda: None),
            ("compose_ps", lambda: {"watcher": "running", "checkin": "running"})]]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        run(["up"])
        self.assertIn(["docker", "compose", "restart", "watcher", "checkin"], self.calls)

    def test_up_no_checkin_turns_it_off_but_keeps_the_key(self):
        self.write(f"COMPOSE_PROFILES=mqtt,checkin\nCHECKIN_KEY=abc\nCHECKIN_PORT={self.port}\nMQTT_PASSWORD=x\n")
        patches = [mock.patch.object(fleet, n, v) for n, v in [
            ("missing_images", lambda: []), ("service_checks", lambda: {"victoriametrics": (True, "healthy")}),
            ("dashboard_state", lambda: "ok"), ("fetch_devices", lambda: {}), ("notify_configured", lambda: None)]]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        code, _, _ = run(["up", "--no-checkin"])
        self.assertEqual(code, 0)
        self.assertIn(["docker", "compose", "--profile", "*", "rm", "-sf", "checkin"], self.calls)
        env = fleet.read_env()
        self.assertEqual(env["COMPOSE_PROFILES"], "mqtt")
        self.assertEqual(env["CHECKIN_KEY"], "abc")  # the commands already handed out work again if it is turned back on
        self.assertFalse(fleet.checkin_enabled())

    def test_service_checks_include_the_service_only_when_it_is_on(self):
        with mock.patch.object(fleet, "vm_healthy", return_value=True), mock.patch.object(fleet, "grafana_healthy", return_value=True):
            self.write("")
            self.assertNotIn("checkin", fleet.service_checks())
            self.write(f"COMPOSE_PROFILES=checkin\nCHECKIN_PORT={self.port}\n")
            self.assertEqual(fleet.service_checks()["checkin"], (True, f"port {self.port} open"))
            self.write("COMPOSE_PROFILES=checkin\nCHECKIN_PORT=1\n")
            self.assertFalse(fleet.service_checks()["checkin"][0])

    def test_down_names_the_service(self):
        with mock.patch.object(fleet, "compose_ps", side_effect=[{"victoriametrics": "running", "checkin": "running"}, {}]), \
                mock.patch.object(fleet, "vm_healthy", return_value=False):
            _, out, _ = run(["down", "--yes"])
        self.assertIn("the dashboard, the collector, the database and the check-in service.", out)

    def test_via_and_check(self):
        self.devices(**{"job": dev(job="checkin", age=3, interval=60)})
        self.assertEqual(fleet.fetch_devices()["job"]["via"], "checkin")
        _, out, _ = run(["check", "job"])
        self.assertIn("via         check-in URL", out)


class FilesTests(unittest.TestCase):
    def block(self):
        with open(os.path.join(ROOT, "docker-compose.yml")) as f:
            text = f.read()
        block = text[text.index("  checkin:\n"):]
        return block[:block.index("\n\n")]

    def test_the_service_is_locked_down(self):
        block = self.block()
        for needle in ['profiles: ["checkin"]', f"image: {fleet.WATCHER_IMAGE}", "read_only: true", "no-new-privileges:true",
                       "- ALL", "./watcher:/app:ro", 'CHECKIN_KEY: ${CHECKIN_KEY:-}', '"${CHECKIN_PORT:-8081}:8081"']:
            self.assertIn(needle, block)
        for forbidden in ("privileged", "network_mode", "NET_RAW", "cap_add", "/var/run/docker.sock"):
            self.assertNotIn(forbidden, block)  # it needs no capability at all, and must not see Docker or the host network

    def test_no_required_variable_can_break_the_default_stack(self):
        with open(os.path.join(ROOT, "docker-compose.yml")) as f:
            self.assertIsNone(re.search(r"\$\{[A-Z_]+:\?", f.read()))


if __name__ == "__main__":
    unittest.main()
