"""Tests for the `fleet` command. Stdlib only. No Docker and no real stack: a fake VictoriaMetrics
HTTP server answers the queries, and subprocess/Docker calls are patched.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_loader = importlib.machinery.SourceFileLoader("fleet", os.path.join(ROOT, "fleet"))
_spec = importlib.util.spec_from_loader("fleet", _loader)
fleet = importlib.util.module_from_spec(_spec)
_loader.exec_module(fleet)
_TMP = tempfile.mkdtemp(prefix="fleet-test-")
fleet.ENV_FILE = os.path.join(_TMP, ".env")  # never read (or write) a real .env


def run(argv):
    """Run the CLI; return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = fleet.main(argv)
        except SystemExit as e:  # argparse errors
            code = e.code
    return code, out.getvalue(), err.getvalue()


# ----------------------------------------------------------------- fake VictoriaMetrics
class FakeVM(BaseHTTPRequestHandler):
    state = {}

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        st = FakeVM.state
        url = urllib.parse.urlparse(self.path)
        if url.path == "/health":
            return self._send(200, b"OK" if st["healthy"] else b"down", "text/plain")
        if url.path != "/api/v1/query":
            return self._send(404)
        expr = urllib.parse.parse_qs(url.query)["query"][0]
        st["queries"].append(expr)
        devices = st["devices"]
        result = []
        if "time() - tlast_over_time" in expr:
            st["age_queries"] += 1
            if st["age_queries"] <= st["hide_until_query"]:
                devices = {}
            result = [{"metric": {"device_id": d}, "value": [0, str(v["age"])]} for d, v in devices.items()]
        elif "topk by" in expr:
            result = [{"metric": {"device_id": d, "hostname": v["host"], "job": v["job"]}, "value": [0, str(v["last"])]}
                      for d, v in devices.items()]
        elif "device_report_interval_seconds" in expr:
            result = [{"metric": {"device_id": d}, "value": [0, str(v["interval"])]}
                      for d, v in devices.items() if v.get("interval") is not None]
        else:
            for key, metric in fleet.READINGS:
                if f"last_over_time({metric}[1m])" in expr:
                    result = [{"metric": {"device_id": d}, "value": [0, str(v[key])]}
                              for d, v in devices.items() if v.get(key) is not None]
        self._send(200, json.dumps({"status": "success", "data": {"resultType": "vector", "result": result}}).encode())

    def do_POST(self):
        st = FakeVM.state
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        if self.path == "/api/v1/admin/tsdb/delete_series":
            matches = urllib.parse.parse_qs(body)["match[]"]
            st["deleted"].append(matches)
            for m in matches:
                if m.startswith('{device_id="'):
                    st["devices"].pop(m.split('"')[1], None)
            return self._send(204)
        if self.path == "/internal/resetRollupResultCache":
            st["cache_reset"] = True
            return self._send(200, b"ok", "text/plain")
        self._send(404)


def dev(age=5.0, host="pi", cpu=1.5, mem=16.0, disk=22.0, temp=47.5, uptime=3600.0, last=1793513000.0, interval=None,
        job="device-agent"):
    return {"age": age, "host": host, "cpu": cpu, "mem": mem, "disk": disk, "temp": temp, "uptime": uptime,
            "last": last, "interval": interval, "job": job}


class WithFakeVM(unittest.TestCase):
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
        FakeVM.state = {"healthy": True, "devices": {}, "queries": [], "deleted": [],
                        "age_queries": 0, "hide_until_query": 0, "cache_reset": False}
        p = mock.patch.object(fleet, "VM_URL", self.url)
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(fleet, "POLL_SECONDS", 0.01)
        q.start()
        self.addCleanup(q.stop)

    def devices(self, **kw):
        FakeVM.state["devices"] = kw


# ----------------------------------------------------------------- pure helpers
class FormatTests(unittest.TestCase):
    def test_fmt_age(self):
        for seconds, want in [(0, "0s"), (59.9, "59s"), (60, "1m 00s"), (125, "2m 05s"), (3700, "1h 01m"),
                              (90000, "1d 1h"), (-5, "0s")]:
            self.assertEqual(fleet.fmt_age(seconds), want, seconds)

    def test_missing_values_show_a_dash(self):
        self.assertEqual((fleet.fmt_pct(None), fleet.fmt_temp(None), fleet.fmt_dur(None)), ("-", "-", "-"))
        self.assertEqual((fleet.fmt_pct(1.55), fleet.fmt_temp(48.04)), ("1.6%", "48.0C"))

    def test_table_aligns_and_handles_no_rows(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fleet.table(["ID", "STATE"], [["a", "ONLINE"], ["longer-id", "OFFLINE"]])
            fleet.table(["ID", "STATE"], [])
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[1].index("ONLINE"), lines[2].index("OFFLINE"))
        self.assertFalse(any(l != l.rstrip() for l in lines), "no trailing spaces")
        self.assertIn("ID", lines[3])

    def test_device_id_rule(self):
        self.assertEqual(fleet.device_id_arg("kitchen-pi.2"), "kitchen-pi.2")
        for bad in ["", "Bad ID", "UPPER", "-lead", 'x"y', "a" * 64, "x}"]:
            with self.assertRaises(Exception, msg=bad):
                fleet.device_id_arg(bad)

    def test_agent_path_is_relative_inside_the_repo(self):
        old = os.getcwd()
        os.chdir(ROOT)
        try:
            self.assertEqual(fleet.agent_path("device_agent.py"), os.path.join("agent", "device_agent.py"))
        finally:
            os.chdir(old)


class ConfirmTests(unittest.TestCase):
    def test_yes_flag_skips_the_question(self):
        self.assertTrue(fleet.confirm("?", assume_yes=True))

    def test_no_terminal_refuses_instead_of_hanging(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            with self.assertRaises(fleet.FleetError) as cm:
                fleet.confirm("?")
        self.assertIn("--yes", str(cm.exception))

    def test_answers(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=True):
            for answer, want in [("y", True), ("YES", True), ("n", False), ("", False), ("maybe", False)]:
                with mock.patch("builtins.input", return_value=answer):
                    self.assertEqual(fleet.confirm("?"), want, answer)
            with mock.patch("builtins.input", return_value="wipe"):
                self.assertTrue(fleet.confirm("?", word="wipe"))
            with mock.patch("builtins.input", return_value="y"):
                self.assertFalse(fleet.confirm("?", word="wipe"))


# ----------------------------------------------------------------- reading devices
class FetchAndListTests(WithFakeVM):
    def test_fetch_devices_state_and_readings(self):
        self.devices(**{"b-pi": dev(age=3, host="hb"), "a-pi": dev(age=120, host="ha", temp=None)})
        got = fleet.fetch_devices()
        self.assertEqual(list(got), ["a-pi", "b-pi"])  # sorted
        self.assertEqual(got["b-pi"]["state"], "ONLINE")
        self.assertEqual(got["a-pi"]["state"], "OFFLINE")
        self.assertEqual(got["a-pi"]["hostname"], "ha")
        self.assertIsNone(got["a-pi"]["temp"])
        self.assertEqual(got["b-pi"]["temp"], 47.5)

    def test_offline_boundary_matches_the_dashboard_rule(self):
        self.devices(**{"on": dev(age=30.0), "off": dev(age=30.1)})
        got = fleet.fetch_devices()
        self.assertEqual((got["on"]["state"], got["off"]["state"]), ("ONLINE", "OFFLINE"))

    def test_a_declared_report_interval_widens_the_offline_limit(self):
        # 3 x the declared interval, never below 30 s. 100 s of silence is fine for a device that reports every minute.
        self.devices(**{"slow-ok": dev(age=100, interval=60), "slow-late": dev(age=181, interval=60),
                        "fast-late": dev(age=30.1, interval=5), "undeclared": dev(age=100)})
        got = fleet.fetch_devices()
        self.assertEqual({k: v["state"] for k, v in got.items()},
                         {"slow-ok": "ONLINE", "slow-late": "OFFLINE", "fast-late": "OFFLINE", "undeclared": "OFFLINE"})
        self.assertEqual((got["slow-ok"]["report_interval"], got["slow-ok"]["offline_after"]), (60.0, 180.0))
        self.assertEqual((got["undeclared"]["report_interval"], got["undeclared"]["offline_after"]), (None, 30))

    def test_offline_limit_rule(self):
        for declared, want in [(None, 30), (float("nan"), 30), (0, 30), (-5, 30), (5, 30), (10, 30),
                               (11, 33), (60, 180), (300, 900)]:
            self.assertEqual(fleet.offline_limit(declared), want, declared)

    def test_check_shows_the_declared_interval(self):
        self.devices(**{"sleepy": dev(age=10, interval=300), "plain": dev(age=10)})
        _, out, _ = run(["check", "sleepy"])
        self.assertIn("every 5m 00s", out)
        self.assertIn("OFFLINE after 15m 00s", out)
        _, out, _ = run(["check", "plain"])
        self.assertNotIn("reports", out)

    def test_list_json(self):
        self.devices(**{"x": dev()})
        code, out, _ = run(["list", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["device_id"], "x")

    def test_list_table(self):
        self.devices(**{"on": dev(age=2), "off": dev(age=300, temp=None)})
        code, out, _ = run(["list"])
        self.assertEqual(code, 0)
        self.assertIn("ONLINE", out)
        self.assertIn("OFFLINE", out)
        self.assertIn("1 online, 1 offline", out)

    def test_list_with_no_devices_points_to_add(self):
        code, out, _ = run(["list"])
        self.assertEqual(code, 0)
        self.assertIn("./fleet add", out)

    def test_check_offline_suggests_remove(self):
        self.devices(old=dev(age=400))
        code, out, _ = run(["check", "old"])
        self.assertEqual(code, 0)
        self.assertIn("OFFLINE", out)
        self.assertIn("./fleet remove old", out)

    def test_check_unknown_device(self):
        code, _, err = run(["check", "ghost"])
        self.assertEqual(code, 1)
        self.assertIn("no device 'ghost'", err)

    def test_everything_needs_a_running_stack(self):
        FakeVM.state["healthy"] = False
        for argv in (["list"], ["check", "x"], ["add", "x"], ["remove", "x"]):
            code, _, err = run(argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("./fleet up", err, argv)


# ----------------------------------------------------------------- add / remove
class AddTests(WithFakeVM):
    def test_refuses_an_id_that_is_already_reporting(self):
        self.devices(taken=dev(age=4))
        code, _, err = run(["add", "taken", "--no-wait"])
        self.assertEqual(code, 1)
        self.assertIn("already reporting", err)

    def test_prints_the_device_commands_with_the_given_host(self):
        code, out, _ = run(["add", "kitchen-pi", "--host", "10.0.0.5", "--no-wait"])
        self.assertEqual(code, 0)
        self.assertIn("--device-id kitchen-pi --endpoint 10.0.0.5:4317", out)
        self.assertIn("scp", out)
        # both ways to run it are offered, and the installer is part of what gets copied over
        self.assertIn("install-service.sh USER@DEVICE", out)
        self.assertIn("sudo bash install-service.sh --device-id kitchen-pi --endpoint 10.0.0.5:4317", out)
        self.assertIn("starts at boot", out)
        self.assertNotIn("not set up to start at boot", out)
        self.assertNotIn("can change", out)  # the hotspot warning only appears for an auto-detected address

    def test_known_offline_device_is_allowed_with_a_note(self):
        self.devices(back=dev(age=500))
        code, out, _ = run(["add", "back", "--host", "h", "--no-wait"])
        self.assertEqual(code, 0)
        self.assertIn("reported before", out)

    def test_waits_until_the_device_appears(self):
        FakeVM.state["hide_until_query"] = 3  # the first three polls see nothing
        self.devices(newdev=dev(age=2))
        code, out, _ = run(["add", "newdev", "--host", "h", "--timeout", "5"])
        self.assertEqual(code, 0)
        self.assertIn("is online", out)

    def test_output_into_a_log_has_no_terminal_control_characters(self):
        # Found by running it for real: a 60-space "\r" wipe was written into piped output.
        FakeVM.state["hide_until_query"] = 1
        self.devices(logged=dev(age=2))
        code, out, _ = run(["add", "logged", "--host", "h", "--timeout", "5"])
        self.assertEqual(code, 0)
        self.assertNotIn("\r", out)
        self.assertTrue(any(l.startswith(("✓", "ok")) for l in out.splitlines()), out)

    def test_gives_up_with_a_hint(self):
        FakeVM.state["hide_until_query"] = 10 ** 6
        code, out, _ = run(["add", "ghost", "--host", "h", "--timeout", "1"])
        self.assertEqual(code, 1)
        self.assertIn("Gave up waiting", out)
        self.assertIn("create_connection", out)


class RemoveTests(WithFakeVM):
    def test_refuses_a_live_device_without_force(self):
        self.devices(live=dev(age=3))
        code, _, err = run(["remove", "live", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("reporting right now", err)
        self.assertEqual(FakeVM.state["deleted"], [])

    def test_deletes_exactly_this_device_and_nothing_else(self):
        self.devices(old=dev(age=900), other=dev(age=2))
        code, out, _ = run(["remove", "old", "--yes"])
        self.assertEqual(code, 0)
        self.assertIn("Removed", out)
        self.assertEqual(FakeVM.state["deleted"],
                         [['{device_id="old"}', '{job="device-agent",instance="old"}']])
        self.assertTrue(FakeVM.state["cache_reset"])
        self.assertIn("other", fleet.fetch_devices())
        self.assertNotIn("old", fleet.fetch_devices())

    def test_declining_changes_nothing(self):
        self.devices(old=dev(age=900))
        with mock.patch.object(fleet, "confirm", return_value=False):
            code, out, _ = run(["remove", "old"])
        self.assertEqual(code, 1)
        self.assertIn("Aborted", out)
        self.assertEqual(FakeVM.state["deleted"], [])

    def test_unknown_device(self):
        code, _, err = run(["remove", "ghost", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("nothing to remove", err)

    def test_force_deletes_a_live_device(self):
        self.devices(live=dev(age=3))
        code, _, _ = run(["remove", "live", "--yes", "--force"])
        self.assertEqual(code, 0)
        self.assertEqual(len(FakeVM.state["deleted"]), 1)


# ----------------------------------------------------------------- down (Docker is patched)
class DownTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.ps = [{"victoriametrics": "running", "grafana": "running", "otel-collector": "running"}, {}]

        def fake_run(cmd, **kw):
            self.calls.append(cmd)
            return mock.Mock(returncode=0)

        for target, new in [("require_docker", lambda: None), ("vm_healthy", lambda: False),
                            ("compose_ps", lambda: self.ps.pop(0) if self.ps else {})]:
            p = mock.patch.object(fleet, target, new)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(fleet.subprocess, "run", fake_run)
        p.start()
        self.addCleanup(p.stop)

    def down_calls(self):
        return [c for c in self.calls if c[:5] == ["docker", "compose", "--profile", "*", "down"]]

    def test_nothing_to_stop(self):
        self.ps = [{}]
        code, out, _ = run(["down", "--yes"])
        self.assertEqual(code, 0)
        self.assertIn("Nothing to stop", out)
        self.assertEqual(self.down_calls(), [])

    def test_saying_no_stops_nothing(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=True), mock.patch("builtins.input", return_value="n"):
            code, out, _ = run(["down"])
        self.assertEqual(code, 1)
        self.assertIn("Aborted", out)
        self.assertEqual(self.down_calls(), [])

    def test_yes_stops_keeps_data_and_confirms(self):
        code, out, _ = run(["down", "--yes"])
        self.assertEqual(code, 0)
        self.assertEqual(self.down_calls(), [["docker", "compose", "--profile", "*", "down"]])  # every profile: no orphans
        self.assertIn("Stack stopped", out)
        self.assertIn("3 container(s) removed", out)
        self.assertIn("kept", out)

    def test_wipe_needs_the_typed_word_and_adds_v(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=True), mock.patch("builtins.input", side_effect=["y", "nope"]):
            code, out, _ = run(["down", "--wipe"])
        self.assertEqual(code, 1)
        self.assertEqual(self.down_calls(), [])
        self.ps = [{"grafana": "running"}, {}]
        with mock.patch.object(sys.stdin, "isatty", return_value=True), mock.patch("builtins.input", side_effect=["y", "wipe"]):
            code, out, _ = run(["down", "--wipe"])
        self.assertEqual(code, 0)
        self.assertEqual(self.down_calls(), [["docker", "compose", "--profile", "*", "down", "-v"]])
        self.assertIn("deleted", out)

    def test_leftover_containers_are_reported_not_hidden(self):
        self.ps = [{"grafana": "running"}, {"grafana": "running"}]
        code, _, err = run(["down", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("still exist", err)


if __name__ == "__main__":
    unittest.main()
