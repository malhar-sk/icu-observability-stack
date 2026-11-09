"""Tests for the devices you expect: `./fleet add` notes them, and list/status/remove show them as WAITING until they report.
Standard library only; the fake VictoriaMetrics from test_fleet answers and every test has its own list of expected devices.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_fleet import FakeVM, WithFakeVM, dev, fleet, run  # noqa: E402
from test_fleet_checkin import WithCheckin  # noqa: E402
from test_fleet_mqtt import WithEnv  # noqa: E402


def stored():
    with open(fleet.EXPECTED_FILE) as f:
        return json.load(f)["devices"]


class NotingADevice(WithFakeVM):
    def test_adding_an_agent_device_notes_it(self):
        code, _, _ = run(["add", "kitchen-pi", "--host", "h", "--no-wait"])
        self.assertEqual(code, 0)
        self.assertEqual(stored()["kitchen-pi"]["kind"], "agent")
        self.assertAlmostEqual(stored()["kitchen-pi"]["added"], time.time(), delta=5)

    def test_adding_again_keeps_the_first_time_and_updates_the_kind(self):
        run(["add", "x", "--host", "h", "--no-wait"])
        first = stored()["x"]["added"]
        fleet.write_expected({"x": {"added": first - 100, "kind": "mqtt"}})
        run(["add", "x", "--host", "h", "--no-wait"])
        self.assertEqual(stored()["x"], {"added": first - 100, "kind": "agent"})

    def test_a_refused_add_notes_nothing(self):
        self.devices(taken=dev(age=3))
        self.assertEqual(run(["add", "taken", "--no-wait"])[0], 1)
        self.assertEqual(run(["add", "Bad Id", "--no-wait"])[0], 2)
        self.assertFalse(os.path.exists(fleet.EXPECTED_FILE))

    def test_a_broken_list_does_not_stop_an_add(self):
        with open(fleet.EXPECTED_FILE, "w") as f:
            f.write("{not json")
        code, out, _ = run(["add", "kitchen-pi", "--host", "h", "--no-wait"])
        self.assertEqual(code, 0)
        self.assertIn("--device-id kitchen-pi", out)
        self.assertIn("Could not note", out)

    def test_the_file_is_written_whole_or_not_at_all(self):
        run(["add", "x", "--host", "h", "--no-wait"])
        self.assertFalse(os.path.exists(fleet.EXPECTED_FILE + ".tmp"))
        with open(fleet.EXPECTED_FILE) as f:
            self.assertEqual(json.load(f)["version"], 1)


class NotingByTheOtherWays(WithEnv, WithFakeVM):
    def setUp(self):
        WithFakeVM.setUp(self)
        WithEnv.setUp(self)

    def test_mqtt_notes_it_only_when_mqtt_is_on(self):
        self.assertEqual(run(["add", "garage", "--mqtt", "--no-wait"])[0], 1)
        self.assertFalse(os.path.exists(fleet.EXPECTED_FILE), "a refused add leaves no ghost")
        self.write("COMPOSE_PROFILES=mqtt\nMQTT_PASSWORD=x\n")
        self.assertEqual(run(["add", "garage", "--mqtt", "--host", "h", "--no-wait"])[0], 0)
        self.assertEqual(stored()["garage"]["kind"], "mqtt")


class NotingByCheckin(WithCheckin):
    def test_checkin_notes_it_only_when_the_request_is_valid(self):
        self.assertEqual(self.add("late", "--checkin", "--every", "1", "--no-wait")[0], 1)
        self.assertFalse(os.path.exists(fleet.EXPECTED_FILE))
        with mock.patch.object(fleet, "ensure_checkin", lambda: ("k" * 24, self.port)):
            self.assertEqual(self.add("backup", "--checkin", "--every", "60", "--host", "h", "--no-wait")[0], 0)
        self.assertEqual(stored()["backup"]["kind"], "checkin")


class ReadingTheList(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        p = mock.patch.object(fleet, "EXPECTED_FILE", os.path.join(self.dir.name, "expected.json"))
        p.start()
        self.addCleanup(p.stop)
        for name, value in (("WATCH_DIR", self.dir.name), ("TARGETS_FILE", os.path.join(self.dir.name, "targets.json"))):
            q = mock.patch.object(fleet, name, value)
            q.start()
            self.addCleanup(q.stop)

    def test_no_file_is_an_empty_list(self):
        self.assertEqual(fleet.read_expected(), {})

    def test_bad_ids_and_bad_entries_are_ignored_not_trusted(self):
        fleet.write_expected({"good": {"kind": "mqtt"}, "Bad Id": {"kind": "agent"}, "x}": {}, "nodict": "text"})
        self.assertEqual(list(fleet.read_expected()), ["good"])

    def test_a_file_that_is_not_a_list_is_an_error_that_says_what_to_do(self):
        for text in ("{not json", "[]", '{"devices": []}', '{"devices": "x"}'):
            with open(fleet.EXPECTED_FILE, "w") as f:
                f.write(text)
            with self.assertRaisesRegex(fleet.FleetError, "Fix it, or delete it"):
                fleet.read_expected()

    def test_waiting_is_expected_or_watched_and_not_reporting(self):
        fleet.write_expected({"new-board": {"kind": "mqtt", "added": 1000}, "up": {"kind": "agent", "added": 5}})
        fleet.write_targets([{"id": "router", "kind": "ping", "target": "10.0.0.1"},
                             {"id": "up", "kind": "tcp", "target": "10.0.0.2:22"}])
        waiting = fleet.waiting_devices({"up": {"state": "ONLINE"}})
        self.assertEqual(list(waiting), ["new-board", "router"], "sorted; a reporting device is never waiting")
        self.assertEqual((waiting["new-board"]["kind"], waiting["new-board"]["added"]), ("mqtt", 1000))
        self.assertEqual((waiting["router"]["kind"], waiting["router"]["added"]), ("watch", None))

    def test_forgetting(self):
        fleet.write_expected({"a": {"kind": "agent"}, "b": {"kind": "agent"}})
        fleet.forget_device("a")
        fleet.forget_device("never-there")
        self.assertEqual(list(fleet.read_expected()), ["b"])


class ListAndStatus(WithFakeVM):
    def test_a_waiting_device_is_listed_with_how_long_and_how_to_leave(self):
        fleet.write_expected({"new-board": {"kind": "mqtt", "added": int(time.time()) - 125}})
        self.devices(up=dev(age=2))
        code, out, _ = run(["list"])
        self.assertEqual(code, 0)
        self.assertIn("Waiting for a first report", out)
        line = next(l for l in out.splitlines() if "new-board" in l)
        self.assertIn("mqtt", line)
        self.assertIn("registered 2m", line)
        self.assertIn("1 online, 0 offline, 1 waiting", out)
        self.assertIn("./fleet remove <id>", out)

    def test_with_nothing_reporting_the_waiting_ones_are_still_shown(self):
        fleet.write_expected({"new-board": {"kind": "agent", "added": int(time.time())}})
        code, out, _ = run(["list"])
        self.assertEqual(code, 0)
        self.assertNotIn("No device has reported", out)
        self.assertIn("new-board", out)
        self.assertNotIn("online,", out, "no empty table and no '0 online' line")

    def test_with_nothing_at_all_the_old_message_is_unchanged(self):
        code, out, _ = run(["list"])
        self.assertIn("No device has reported in the last 24 hours.  Add one with: ./fleet add <device-id>", out)

    def test_json_keeps_its_shape_for_scripts(self):
        fleet.write_expected({"new-board": {"kind": "agent", "added": 5}})
        self.devices(up=dev(age=2))
        _, out, _ = run(["list", "--json"])
        self.assertEqual([d["device_id"] for d in json.loads(out)], ["up"])

    def test_a_device_that_reports_stops_waiting(self):
        fleet.write_expected({"board": {"kind": "agent", "added": 5}})
        self.assertIn("Waiting for a first report", run(["list"])[1])
        self.devices(board=dev(age=2))
        out = run(["list"])[1]
        self.assertNotIn("Waiting for a first report", out)
        self.assertIn("1 online, 0 offline", out)

    def test_a_device_silent_for_over_a_day_is_waiting_again_not_invisible(self):
        fleet.write_expected({"old": {"kind": "agent", "added": 5}})  # reported once; the 24 h look-back no longer sees it
        self.assertIn("old", run(["list"])[1])

    def test_a_broken_list_is_a_warning_and_the_rest_still_works(self):
        with open(fleet.EXPECTED_FILE, "w") as f:
            f.write("nonsense")
        self.devices(up=dev(age=2))
        code, out, _ = run(["list"])
        self.assertEqual(code, 0)
        self.assertIn("Could not read the list of expected devices", out)
        self.assertIn("up", out)

    def test_status_counts_them(self):
        fleet.write_expected({"board": {"kind": "agent", "added": 5}})
        self.devices(up=dev(age=2))
        with mock.patch.object(fleet, "require_docker", lambda: None), \
             mock.patch.object(fleet, "compose_ps", lambda: {"victoriametrics": "running"}), \
             mock.patch.object(fleet, "service_checks", lambda: {"victoriametrics": (True, "healthy")}), \
             mock.patch.object(fleet, "expected_services", lambda: ["victoriametrics"]), \
             mock.patch.object(fleet, "dashboard_state", lambda: "ok"), \
             mock.patch.object(fleet, "notify_configured", lambda: None):
            _, out, _ = run(["status"])
        self.assertIn("1 online, 0 offline, 1 waiting", out)


class RemovingAWaitingDevice(WithFakeVM):
    def test_it_is_forgotten_after_asking_and_nothing_is_deleted_from_the_database(self):
        fleet.write_expected({"board": {"kind": "mqtt", "added": 5}, "other": {"kind": "agent", "added": 5}})
        code, out, _ = run(["remove", "board", "--yes"])
        self.assertEqual(code, 0)
        self.assertIn("only stops expecting it", out)
        self.assertEqual(list(fleet.read_expected()), ["other"])
        self.assertEqual(FakeVM.state["deleted"], [])

    def test_declining_changes_nothing(self):
        fleet.write_expected({"board": {"kind": "agent", "added": 5}})
        with mock.patch.object(fleet, "confirm", return_value=False):
            code, out, _ = run(["remove", "board"])
        self.assertEqual(code, 1)
        self.assertIn("Aborted", out)
        self.assertIn("board", fleet.read_expected())

    def test_removing_a_reporting_device_also_forgets_it(self):
        fleet.write_expected({"old": {"kind": "agent", "added": 5}})
        self.devices(old=dev(age=900))
        self.assertEqual(run(["remove", "old", "--yes"])[0], 0)
        self.assertEqual(fleet.read_expected(), {})

    def test_an_unknown_id_is_still_an_error(self):
        fleet.write_expected({"board": {"kind": "agent", "added": 5}})
        code, _, err = run(["remove", "ghost", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("none is registered", err)

    def test_a_watched_target_still_points_to_watch_remove(self):
        fleet.write_targets([{"id": "router", "kind": "ping", "target": "10.0.0.1"}])
        code, _, err = run(["remove", "router", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("./fleet watch remove router", err)


if __name__ == "__main__":
    unittest.main()
