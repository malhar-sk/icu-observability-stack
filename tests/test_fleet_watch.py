"""Tests for `fleet watch ...` and the other places the watcher touches the CLI. Stdlib only. No Docker and no real
watcher: subprocess calls are patched and a fake VictoriaMetrics answers the queries. The live path (real containers,
real exporters, the real alert) is covered by scripts/validate_watch.sh.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import json
import os
import re
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_fleet import FakeVM, WithFakeVM, dev, fleet, run  # noqa: E402
from test_fleet_mqtt import WithEnv  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class WithWatch(WithEnv, WithFakeVM):
    """A fake VictoriaMetrics, a private .env and a private target list."""

    def setUp(self):
        WithFakeVM.setUp(self)
        WithEnv.setUp(self)
        self.watch_dir = os.path.join(self.dir.name, "watch")
        for name, value in [("WATCH_DIR", self.watch_dir), ("TARGETS_FILE", os.path.join(self.watch_dir, "targets.json")),
                            ("WATCH_FIRST_LOOK", 0), ("WATCH_LOOK_EVERY", 0), ("WATCH_SETTLE", 0), ("require_docker", lambda: None)]:
            p = mock.patch.object(fleet, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.log = ""
        p = mock.patch.object(fleet, "compose", lambda *a, **k: mock.Mock(returncode=0, stdout=self.log, stderr=""))
        p.start()
        self.addCleanup(p.stop)

    def targets(self):
        if not os.path.exists(fleet.TARGETS_FILE):
            return []
        with open(fleet.TARGETS_FILE) as f:
            return json.load(f)["targets"]

    def seed(self, *specs):
        fleet.write_targets(list(specs))

    def docker_calls(self):
        return [c for c in self.calls if c[:2] == ["docker", "compose"]]


class TargetFileTests(WithWatch):
    def test_missing_file_is_an_empty_list(self):
        self.assertEqual(fleet.read_targets(), [])

    def test_write_is_atomic_readable_by_the_container_and_round_trips(self):
        fleet.write_targets([{"id": "a", "kind": "ping", "target": "x"}])
        self.assertEqual(fleet.read_targets(), [{"id": "a", "kind": "ping", "target": "x"}])
        self.assertEqual(stat.S_IMODE(os.stat(fleet.TARGETS_FILE).st_mode), 0o644)  # the container user is not the owner
        self.assertFalse(os.path.exists(fleet.TARGETS_FILE + ".tmp"))

    def test_a_broken_file_is_explained_not_overwritten(self):
        os.makedirs(self.watch_dir)
        for text in ["{nope", "[]", '{"targets": "x"}']:
            with open(fleet.TARGETS_FILE, "w") as f:
                f.write(text)
            with self.assertRaises(fleet.FleetError, msg=text) as cm:
                fleet.read_targets()
            self.assertIn("delete it", str(cm.exception))
            with open(fleet.TARGETS_FILE) as f:
                self.assertEqual(f.read(), text)

    def test_an_unwritable_folder_is_explained(self):
        with mock.patch.object(fleet.os, "makedirs", side_effect=PermissionError("denied")):
            with self.assertRaises(fleet.FleetError) as cm:
                fleet.write_targets([])
        self.assertIn("remove it", str(cm.exception))

    def test_the_cli_validates_with_the_services_own_rules(self):
        mod = fleet.watcher_module()
        self.assertEqual(mod.KINDS, ("ping", "tcp", "http", "metrics"))
        self.assertIs(fleet.watcher_module(), mod)  # loaded once
        sys.path.insert(0, os.path.join(ROOT, "agent"))
        from device_agent import DEVICE_ID_RE
        self.assertEqual(mod.DEVICE_ID_RE.pattern, DEVICE_ID_RE.pattern)


class AddTests(WithWatch):
    def add(self, *argv):
        return run(["watch", "add", *argv])

    def test_a_ping_target_is_saved_with_defaults_and_the_watcher_is_started(self):
        code, out, _ = self.add("router", "ping", "192.168.1.1", "--no-wait")
        self.assertEqual(code, 0)
        self.assertEqual(self.targets(), [{"id": "router", "kind": "ping", "target": "192.168.1.1", "interval": 15,
                                           "timeout": 5, "insecure": False, "expect_status": None, "max_series": None}])
        self.assertIn(["docker", "compose", "up", "-d", "watcher"], self.calls)
        self.assertEqual(fleet.read_env()["COMPOSE_PROFILES"], "watch")
        self.assertIn("OFFLINE after 45s", out)  # 3 x the 15 s interval
        self.assertTrue(fleet.watch_enabled())

    def test_options_are_passed_on(self):
        self.add("web", "http", "https://10.0.0.1:8443/", "--interval", "30", "--timeout", "10", "--insecure",
                 "--expect-status", "401", "--no-wait")
        t = self.targets()[0]
        self.assertEqual((t["interval"], t["timeout"], t["insecure"], t["expect_status"]), (30, 10, True, 401))
        self.assertIsInstance(t["interval"], int)  # 30, not 30.0

    def test_a_metrics_target_gets_the_default_limit_or_the_one_asked_for(self):
        self.add("esp", "metrics", "http://10.0.0.5/metrics", "--no-wait")
        self.add("big", "metrics", "http://10.0.0.6/metrics", "--max-series", "5000", "--no-wait")
        self.assertEqual([t["max_series"] for t in self.targets()], [2000, 5000])
        code, _, err = self.add("bad", "metrics", "http://10.0.0.7/metrics", "--max-series", "999999", "--no-wait")
        self.assertEqual(code, 1)
        self.assertIn("max_series must be", err)
        code, _, err = self.add("bad2", "ping", "10.0.0.8", "--max-series", "10", "--no-wait")
        self.assertIn("only applies to metrics", err)

    def test_a_fast_interval_still_waits_for_the_floor_before_calling_it_offline(self):
        _, out, _ = self.add("fast", "tcp", "nas.lan:445", "--interval", "5", "--no-wait")
        self.assertIn("OFFLINE after 30s", out)

    def test_bad_input_is_refused_before_anything_changes(self):
        for argv, text in [(("Router", "ping", "x"), "invalid device id"), (("r", "udp", "x"), "is not a kind"),
                           (("r", "tcp", "host"), "host:port"),
                           (("r", "http", "ftp://x"), "http:// or https://"), (("r", "ping", "x", "--interval", "1"), "between 5 and 3600"),
                           (("r", "ping", "x", "--insecure"), "only apply to http"), (("r", "http", "http://x/", "--insecure"), "https"),
                           (("r", "metrics", "http://x/m", "--expect-status", "200"), "expect_status does not apply")]:
            code, _, err = self.add(*argv, "--no-wait")
            self.assertNotEqual(code, 0, argv)
            self.assertIn(text, err, argv)
        self.assertEqual(self.targets(), [])
        self.assertEqual(self.docker_calls(), [])
        self.assertFalse(os.path.exists(self.env_path))

    def test_leaving_out_the_kind_says_what_the_command_looks_like(self):
        code, _, err = run(["watch", "add", "router", "10.0.0.1"])
        self.assertNotEqual(code, 0)
        self.assertIn("is not a kind", err)
        self.assertIn("./fleet watch add router ping 10.0.0.1", err)  # the fix, with their own address filled in
        for address in ("nas.lan", "192.168.1.5", "host:80", "http://x/", "5"):
            self.assertIn("looks like the kind was left out", run(["watch", "add", "r", address])[2], address)
        code, _, err = run(["watch", "add", "r", "udp", "x"])  # a wrong kind is not an address: no misleading example
        self.assertIn("choose ping, tcp, http or metrics", err)
        self.assertNotIn("left out", err)
        self.assertNotIn("ping udp", err)

    def test_a_target_that_looks_like_an_option_cannot_reach_ping(self):
        # argparse refuses it outright; and if it is forced through with `--`, the validation does
        code, _, err = run(["watch", "add", "--no-wait", "r", "ping", "-c"])
        self.assertNotEqual(code, 0)
        code, _, err = run(["watch", "add", "--no-wait", "r", "ping", "--", "-c"])
        self.assertEqual(code, 1)
        self.assertIn("not a host name", err)
        self.assertEqual(self.targets(), [])

    def test_an_id_that_is_already_watched_is_refused(self):
        self.add("router", "ping", "192.168.1.1", "--no-wait")
        code, _, err = self.add("router", "ping", "192.168.1.2", "--no-wait")
        self.assertEqual(code, 1)
        self.assertIn("already watched", err)
        self.assertEqual(len(self.targets()), 1)

    def test_an_id_that_is_reporting_through_something_else_is_refused(self):
        self.devices(**{"pi": dev(age=3, job="device-agent")})
        code, _, err = self.add("pi", "ping", "10.0.0.2", "--no-wait")
        self.assertEqual(code, 1)
        self.assertIn("already reporting (via agent", err)
        self.assertEqual(self.targets(), [])

    def test_an_offline_id_may_be_reused(self):
        self.devices(**{"old": dev(age=900, job="device-agent")})
        self.assertEqual(self.add("old", "ping", "10.0.0.2", "--no-wait")[0], 0)

    def test_a_running_watcher_is_not_started_again(self):
        with mock.patch.object(fleet, "compose_ps", lambda: {"watcher": "running"}):
            self.add("router", "ping", "192.168.1.1", "--no-wait")
        self.assertNotIn(["docker", "compose", "up", "-d", "watcher"], self.calls)

    def test_other_profiles_survive(self):
        self.write("COMPOSE_PROFILES=mqtt\nMQTT_PASSWORD=x\n")
        self.add("router", "ping", "192.168.1.1", "--no-wait")
        self.assertEqual(fleet.read_env()["COMPOSE_PROFILES"], "mqtt,watch")
        self.assertEqual(fleet.mqtt_mode(), "bundled")  # the MQTT setting is untouched

    def test_a_local_name_is_called_out(self):
        for kind, target in [("ping", "raspberrypi.local"), ("tcp", "esp32.local:80"), ("metrics", "http://esp32.local/metrics")]:
            _, out, _ = self.add(f"t-{kind}", kind, target, "--no-wait")
            self.assertIn(".local name", out, kind)
        _, out, _ = self.add("plain", "ping", "192.168.1.9", "--no-wait")
        self.assertNotIn(".local", out)

    def test_it_waits_for_the_first_answer(self):
        FakeVM.state["hide_until_query"] = 3
        self.devices(**{"router": dev(age=2, job="watcher", cpu=None, mem=None, temp=None)})
        code, out, _ = self.add("router", "ping", "192.168.1.1", "--wait", "5")
        self.assertEqual(code, 0)
        self.assertIn("router answered after", out)
        self.assertIn("./fleet check router", out)

    def test_a_target_that_does_not_answer_says_why_without_waiting_for_the_timeout(self):
        self.log = ("2026-11-02 15:19:12,819 INFO watching 1 target(s)\n"
                    "2026-11-02 15:19:13,819 WARNING router: DOWN (ping 192.168.1.1: no reply)\n")
        code, out, _ = self.add("router", "ping", "192.168.1.1", "--wait", "30")
        self.assertEqual(code, 1)
        self.assertIn("Not answering: ping 192.168.1.1: no reply", out)
        self.assertIn("./fleet watch remove router", out)
        self.assertEqual([t["id"] for t in self.targets()], ["router"])  # it stays: it may simply be switched off

    def test_a_target_that_came_up_after_being_down_is_not_reported_as_failing(self):
        self.log = ("2026-11-02 15:19:13,819 WARNING router: DOWN (ping 1.1.1.1: no reply)\n"
                    "2026-11-02 15:19:30,819 INFO router: UP (ping 1.1.1.1: 2 ms)\n")
        self.assertIsNone(fleet.watch_failure("router"))
        self.assertEqual(fleet.watch_failure("other"), None)

    def test_the_log_match_is_exact_for_the_id(self):
        self.log = "2026-11-02 15:19:13,819 WARNING router-2: DOWN (ping x: no reply)\n"
        self.assertIsNone(fleet.watch_failure("router"))
        self.assertEqual(fleet.watch_failure("router-2"), "ping x: no reply")

    def test_no_answer_at_all_gives_a_pointer_to_the_log(self):
        code, out, _ = self.add("router", "ping", "192.168.1.1", "--wait", "1")
        self.assertEqual(code, 1)
        self.assertIn("docker compose logs watcher", out)

    def test_it_needs_a_running_stack(self):
        FakeVM.state["healthy"] = False
        code, _, err = self.add("router", "ping", "x", "--no-wait")
        self.assertEqual(code, 1)
        self.assertIn("not running", err)
        self.assertEqual(self.targets(), [])


class ListTests(WithWatch):
    def test_empty(self):
        code, out, _ = run(["watch", "list"])
        self.assertEqual(code, 0)
        self.assertIn("Nothing is being watched", out)

    def test_states(self):
        self.seed(dict(id="router", kind="ping", target="192.168.1.1", interval=15),
                  dict(id="nas", kind="tcp", target="nas.lan:445", interval=30),
                  dict(id="ghost", kind="http", target="http://x/", interval=15))
        self.write("COMPOSE_PROFILES=watch\n")
        self.devices(**{"router": dev(age=4, job="watcher"), "nas": dev(age=300, job="watcher")})
        code, out, _ = run(["watch", "list"])
        self.assertEqual(code, 0)
        rows = {l.split()[0]: l for l in out.splitlines() if l.strip().split()[:1] and l.split()[0] in ("router", "nas", "ghost")}
        self.assertIn("ONLINE", rows["router"])
        self.assertIn("OFFLINE", rows["nas"])
        self.assertIn("waiting", rows["ghost"])
        self.assertIn("'waiting' = it has not answered yet", out)
        self.assertNotIn("not turned on", out)

    def test_it_says_when_the_watcher_is_off(self):
        self.seed(dict(id="router", kind="ping", target="x", interval=15))
        _, out, _ = run(["watch", "list"])
        self.assertIn("not turned on", out)

    def test_json(self):
        self.seed(dict(id="router", kind="ping", target="192.168.1.1", interval=15))
        self.devices(**{"router": dev(age=4, job="watcher")})
        _, out, _ = run(["watch", "list", "--json"])
        data = json.loads(out)
        self.assertEqual((data[0]["id"], data[0]["state"]), ("router", "ONLINE"))

    def test_it_works_without_the_stack(self):
        FakeVM.state["healthy"] = False
        self.seed(dict(id="router", kind="ping", target="x", interval=15))
        code, out, _ = run(["watch", "list"])
        self.assertEqual(code, 0)
        self.assertIn("router", out)


class RemoveTests(WithWatch):
    def seeded(self, *ids):
        self.seed(*[dict(id=i, kind="ping", target=f"10.0.0.{n}", interval=15) for n, i in enumerate(ids, 1)])

    def test_unknown_id(self):
        code, _, err = run(["watch", "remove", "nope", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("not being watched", err)

    def test_declining_changes_nothing(self):
        self.seeded("a")
        with mock.patch.object(fleet, "confirm", return_value=False):
            code, out, _ = run(["watch", "remove", "a"])
        self.assertEqual(code, 1)
        self.assertIn("Aborted", out)
        self.assertEqual([t["id"] for t in self.targets()], ["a"])
        self.assertEqual(FakeVM.state["deleted"], [])

    def test_it_stops_watching_and_deletes_exactly_this_devices_data(self):
        self.seeded("a", "b")
        self.devices(**{"a": dev(job="watcher"), "b": dev(job="watcher")})
        self.write("COMPOSE_PROFILES=watch\n")
        code, out, _ = run(["watch", "remove", "a", "--yes"])
        self.assertEqual(code, 0)
        self.assertEqual([t["id"] for t in self.targets()], ["b"])
        self.assertEqual(FakeVM.state["deleted"], [['{device_id="a"}', '{job="device-agent",instance="a"}']])
        self.assertNotIn("a", fleet.fetch_devices())
        self.assertIn("b", fleet.fetch_devices())
        self.assertNotIn(["docker", "compose", "--profile", "*", "rm", "-sf", "watcher"], self.calls)  # b still needs it
        self.assertTrue(fleet.watch_enabled())

    def test_the_last_target_takes_the_watcher_with_it(self):
        self.seeded("a")
        self.write("COMPOSE_PROFILES=mqtt,watch\nMQTT_PASSWORD=x\n")
        with mock.patch.object(fleet, "compose_ps", lambda: {"watcher": "running"}):
            code, out, _ = run(["watch", "remove", "a", "--yes"])
        self.assertEqual(code, 0)
        self.assertEqual(self.targets(), [])
        self.assertIn(["docker", "compose", "--profile", "*", "rm", "-sf", "watcher"], self.calls)
        self.assertEqual(fleet.read_env()["COMPOSE_PROFILES"], "mqtt")  # MQTT is left alone
        self.assertIn("watcher was stopped", out)

    def test_data_that_comes_back_is_deleted_again(self):
        self.seeded("a")
        self.devices(**{"a": dev(job="watcher")})
        original = fleet.delete_device_data
        calls = []

        def stubborn(device_id):  # the first delete loses a race against a check that was already running
            calls.append(device_id)
            if len(calls) > 1:
                original(device_id)
        with mock.patch.object(fleet, "delete_device_data", stubborn):
            run(["watch", "remove", "a", "--yes"])
        self.assertEqual(calls, ["a", "a"])

    def test_plain_remove_refuses_a_watched_id(self):
        self.seeded("router")
        self.devices(**{"router": dev(age=500, job="watcher")})
        code, _, err = run(["remove", "router", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("./fleet watch remove router", err)
        self.assertEqual(FakeVM.state["deleted"], [])


class DeviceViewTests(WithWatch):
    def test_via_comes_from_the_job_label(self):
        self.devices(**{"w": dev(job="watcher"), "m": dev(job="mqtt-bridge"), "a": dev()})
        got = fleet.fetch_devices()
        self.assertEqual((got["w"]["via"], got["m"]["via"], got["a"]["via"]), ("watcher", "mqtt", "agent"))

    def test_check_shows_what_is_watched(self):
        self.seed(dict(id="router", kind="ping", target="192.168.1.1", interval=15))
        self.devices(**{"router": dev(job="watcher", interval=15)})
        _, out, _ = run(["check", "router"])
        self.assertIn("via         watcher (ping 192.168.1.1)", out)
        self.assertIn("reports     every 15s  (OFFLINE after 45s", out)

    def test_waiting_line_does_not_invent_readings(self):
        self.devices(**{"router": dev(age=2, job="watcher", cpu=None, mem=None, temp=None)})
        code, out, _ = run(["add", "router", "--mqtt", "--no-wait"])  # unrelated flow, just to reach wait_for_device below
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertTrue(fleet.wait_for_device("router", 2))
        self.assertIn("(via watcher)", buf.getvalue())
        self.assertNotIn("CPU -", buf.getvalue())
        self.devices(**{"pi": dev(age=2)})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fleet.wait_for_device("pi", 2)
        self.assertIn("CPU 1.5%", buf.getvalue())

    def test_add_points_at_watching_for_things_that_cannot_run_the_agent(self):
        _, out, _ = run(["add", "nas", "--host", "10.0.0.5", "--no-wait"])
        self.assertIn("./fleet watch add nas ping <address>", out)


class StackTests(WithWatch):
    def test_service_checks_include_the_watcher_only_when_it_is_on(self):
        with mock.patch.object(fleet, "vm_healthy", return_value=True), mock.patch.object(fleet, "grafana_healthy", return_value=True), \
                mock.patch.object(fleet, "port_open", return_value=True), mock.patch.object(fleet, "compose_ps", lambda: {"watcher": "running"}):
            self.assertNotIn("watcher", fleet.service_checks())
            self.write("COMPOSE_PROFILES=watch\n")
            self.assertEqual(fleet.service_checks()["watcher"], (True, "running"))

    def test_down_names_what_it_stops(self):
        with mock.patch.object(fleet, "compose_ps", side_effect=[{"victoriametrics": "running", "watcher": "running", "mosquitto": "running"}, {}]), \
                mock.patch.object(fleet, "vm_healthy", return_value=False):
            _, out, _ = run(["down", "--yes"])
        self.assertIn("the dashboard, the collector, the database, the MQTT broker and the watcher.", out)

    def test_down_for_the_plain_stack_reads_as_before(self):
        with mock.patch.object(fleet, "compose_ps", side_effect=[{"victoriametrics": "running"}, {}]), \
                mock.patch.object(fleet, "vm_healthy", return_value=False):
            _, out, _ = run(["down", "--yes"])
        self.assertIn("This will stop the dashboard, the collector and the database.", out)

    def test_up_restarts_a_running_watcher_so_it_runs_the_current_code(self):
        self.write("COMPOSE_PROFILES=watch\n")
        patches = [mock.patch.object(fleet, n, v) for n, v in [
            ("missing_images", lambda: []), ("service_checks", lambda: {"victoriametrics": (True, "healthy")}),
            ("dashboard_state", lambda: "ok"), ("lan_ip", lambda: "10.0.0.5"), ("fetch_devices", lambda: {}),
            ("notify_configured", lambda: None), ("compose_ps", lambda: {"watcher": "running"})]]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        run(["up"])
        self.assertIn(["docker", "compose", "restart", "watcher"], self.calls)
        self.calls.clear()
        with mock.patch.object(fleet, "compose_ps", lambda: {}):  # not running yet: `up` creates it fresh
            run(["up"])
        self.assertNotIn(["docker", "compose", "restart", "watcher"], self.calls)

    def test_the_small_images_are_not_counted_as_the_big_download(self):
        with mock.patch.object(fleet, "missing_images", lambda: ["python:3.13-alpine"]), \
                mock.patch.object(fleet, "service_checks", lambda: {"victoriametrics": (True, "healthy")}), \
                mock.patch.object(fleet, "dashboard_state", lambda: "ok"), mock.patch.object(fleet, "lan_ip", lambda: "10.0.0.5"), \
                mock.patch.object(fleet, "fetch_devices", lambda: {}), mock.patch.object(fleet, "notify_configured", lambda: None):
            _, out, _ = run(["up"])
        self.assertIn("1 small image(s)", out)
        self.assertNotIn("590 MB", out)


class FilesTests(unittest.TestCase):
    def compose_text(self):
        with open(os.path.join(ROOT, "docker-compose.yml")) as f:
            return f.read()

    def test_the_watcher_service_is_locked_down(self):
        text = self.compose_text()
        block = text[text.index("  watcher:\n"):]
        block = block[:block.index("\n\n")]
        for needle in ['profiles: ["watch"]', f"image: {fleet.WATCHER_IMAGE}", "read_only: true", "no-new-privileges:true",
                       "- ALL", "- NET_RAW", "./watcher:/app:ro", "./watch:/watch:ro", "TARGETS_FILE: /watch/targets.json"]:
            self.assertIn(needle, block)
        self.assertNotIn("privileged", block)
        self.assertNotIn("network_mode", block)
        self.assertNotIn("ports:", block)  # nothing is published

    def test_the_target_list_is_not_committed(self):
        with open(os.path.join(ROOT, ".gitignore")) as f:
            self.assertIn("watch/", f.read().split())

    def test_the_service_and_the_cli_agree_on_the_file_name(self):
        self.assertTrue(fleet.TARGETS_FILE.endswith(os.path.join("watch", "targets.json")))
        self.assertIn("/watch/targets.json", self.compose_text())

    def test_the_watch_profile_does_not_disturb_the_default_stack(self):
        self.assertIsNone(re.search(r"\$\{[A-Z_]+:\?", self.compose_text()))


if __name__ == "__main__":
    unittest.main()
