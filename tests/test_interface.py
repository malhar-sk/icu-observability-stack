"""Tests for interface/: the registry, the features, the secrets guard and the read-only HTTP API. Standard library only.
No Docker and no real stack: the fake VictoriaMetrics from test_fleet answers, and `fleet`'s checks of other services
are patched. The MCP server has its own file (test_interface_mcp.py) because it needs the `mcp` package.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import http.client
import io
import json
import os
import re
import shlex
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_fleet import FakeVM, WithFakeVM, dev, fleet  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from interface import fleetlink, http_api, registry  # noqa: E402
from interface.registry import FeatureError, SecretLeak  # noqa: E402

registry.load_features()  # declares the real features once, for every test below
REAL_FEATURES = dict(registry._features)
SECRET = "s3cret-value-0123456789"


class Isolated(unittest.TestCase):
    """The adapter reads the test module of ./fleet, with a private .env, and no other service answers."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env_path = os.path.join(self.dir.name, ".env")
        for target, name, value in [(fleetlink, "_module", fleet), (fleet, "ENV_FILE", self.env_path),
                                    (fleet, "port_open", lambda host, port, timeout=2: False),
                                    (fleet, "grafana_healthy", lambda: False),
                                    (fleet, "lan_ip", lambda: "192.168.1.10"),
                                    (fleet, "TARGETS_FILE", os.path.join(self.dir.name, "targets.json")),
                                    (fleet, "WATCH_DIR", self.dir.name),
                                    (fleet, "EXPECTED_FILE", os.path.join(self.dir.name, "expected.json"))]:
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {}, clear=False)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop(registry.DISABLE_ENV, None)

    def env(self, text):
        with open(self.env_path, "w") as f:
            f.write(text)


class Clean(unittest.TestCase):
    """A registry with nothing in it, so a test can declare its own features."""

    def setUp(self):
        for target, name, value in [(registry, "_features", {}), (registry, "_secret_sources", [])]:
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop(registry.DISABLE_ENV, None)


# ----------------------------------------------------------------------------- the registry
class DeclaringFeatures(Clean):
    def test_a_feature_is_listed_with_its_summary_and_parameters(self):
        @registry.feature
        def ask(name: str, loud: bool = False, times: int = 2) -> dict:
            """Say hello.

            More detail on the second line."""
            return {}
        f = registry.get("ask")
        self.assertEqual(f.summary, "Say hello.")
        self.assertIn("More detail", f.description)
        self.assertEqual([(p.name, p.type, p.required) for p in f.params],
                         [("name", str, True), ("loud", bool, False), ("times", int, False)])
        d = registry.describe(f)
        self.assertEqual(d["parameters"][2], {"name": "times", "type": "int", "required": False, "default": 2})

    def test_a_feature_without_a_docstring_is_refused(self):
        with self.assertRaisesRegex(TypeError, "docstring"):
            @registry.feature
            def silent() -> dict:
                return {}

    def test_parameters_need_a_simple_type_hint(self):
        with self.assertRaisesRegex(TypeError, "type hint"):
            @registry.feature
            def untyped(x) -> dict:
                """Doc."""
                return {}
        with self.assertRaisesRegex(TypeError, "type hint"):
            @registry.feature
            def listy(x: list) -> dict:
                """Doc."""
                return {}
        with self.assertRaisesRegex(TypeError, "name every parameter"):
            @registry.feature
            def starry(*args: str) -> dict:
                """Doc."""
                return {}

    def test_declaring_a_name_twice_is_refused(self):
        @registry.feature
        def one() -> dict:
            """Doc."""
            return {}
        with self.assertRaisesRegex(ValueError, "declared twice"):
            registry.feature(one)


class CallingFeatures(Clean):
    def setUp(self):
        super().setUp()

        @registry.feature
        def echo(word: str, count: int = 1) -> dict:
            """Repeat a word."""
            return {"said": word * count}

        @registry.feature
        def returns_a_list() -> dict:
            """Doc."""
            return [1]

        @registry.feature
        def returns_nan() -> dict:
            """Doc."""
            return {"x": float("nan")}

    def test_call_runs_the_feature(self):
        self.assertEqual(registry.call("echo", word="ab", count=2), {"said": "abab"})

    def test_a_wrong_call_is_a_plain_message(self):
        for kwargs, words in [({}, "needs: word"), ({"word": "a", "bogus": 1}, "no parameter bogus")]:
            with self.assertRaisesRegex(FeatureError, words):
                registry.call("echo", **kwargs)
        with self.assertRaises(FeatureError) as cm:
            registry.call("missing")
        self.assertEqual(cm.exception.code, 404)

    def test_a_feature_must_return_a_json_dict(self):
        with self.assertRaisesRegex(TypeError, "must return a dict"):
            registry.call("returns_a_list")
        with self.assertRaisesRegex(TypeError, "not valid JSON"):
            registry.call("returns_nan")

    def test_query_strings_become_the_declared_types(self):
        f = registry.get("echo")
        self.assertEqual(registry.coerce(f, {"word": "a", "count": "3"}), {"word": "a", "count": 3})
        with self.assertRaisesRegex(FeatureError, "'count' must be a whole number"):
            registry.coerce(f, {"count": "x"})

        @registry.feature
        def flags(on: bool, ratio: float = 0.5) -> dict:
            """Doc."""
            return {}
        g = registry.get("flags")
        self.assertEqual(registry.coerce(g, {"on": "TRUE", "ratio": "1.5"}), {"on": True, "ratio": 1.5})
        self.assertEqual(registry.coerce(g, {"on": "0"}), {"on": False})
        with self.assertRaisesRegex(FeatureError, "true or false"):
            registry.coerce(g, {"on": "maybe"})


class SwitchingFeaturesOff(Clean):
    def setUp(self):
        super().setUp()
        @registry.feature
        def alpha() -> dict:
            """Doc."""
            return {}

        @registry.feature
        def beta() -> dict:
            """Doc."""
            return {}

    def test_the_switch_removes_a_feature_everywhere(self):
        os.environ[registry.DISABLE_ENV] = "beta"
        self.assertEqual([f.name for f in registry.enabled()], ["alpha"])
        self.assertIsNone(registry.get("beta"))
        with self.assertRaises(FeatureError):
            registry.call("beta")

    def test_a_typo_in_the_switch_is_an_error_not_a_silent_no_op(self):
        os.environ[registry.DISABLE_ENV] = "alpah"
        with self.assertRaisesRegex(ValueError, "alpah.*not a feature.*alpha, beta"):
            registry.enabled()

    def test_spaces_and_empty_entries_are_fine(self):
        os.environ[registry.DISABLE_ENV] = " alpha , ,"
        self.assertEqual([f.name for f in registry.enabled()], ["beta"])


class TheSecretsGuard(Clean):
    def setUp(self):
        super().setUp()
        registry.register_secret_source(lambda: {SECRET, "short", ""})

    def declare(self, value):
        registry._features.pop("leaky", None)

        def leaky() -> dict:
            """Doc."""
            return value
        registry.feature(leaky)

    def test_a_secret_in_an_answer_is_never_returned(self):
        for value in [{"a": f"password is {SECRET}"}, {"a": {"b": [1, {"c": SECRET}]}}, {"a": [SECRET]}]:
            self.declare(value)
            with self.assertRaises(SecretLeak, msg=value) as cm:
                registry.call("leaky")
            self.assertNotIn(SECRET, str(cm.exception), "the message must not repeat the secret")

    def test_ordinary_answers_pass_and_short_values_are_not_treated_as_secrets(self):
        self.declare({"a": "no secret here", "b": "short", "c": 5})
        self.assertEqual(registry.call("leaky")["b"], "short")


class StrictJsonArguments(Clean):
    """Arguments from the MCP server arrive as JSON values and are checked more strictly than a query string."""

    def setUp(self):
        super().setUp()

        @registry.feature
        def typed(text: str, count: int = 1, ratio: float = 0.5, on: bool = False) -> dict:
            """Doc."""
            return {"got": [text, count, ratio, on]}
        self.f = registry.get("typed")

    def test_right_types_pass_and_nothing_is_added(self):
        given = {"text": "a", "count": 3, "ratio": 2, "on": True}
        self.assertEqual(registry.from_json(self.f, given), given)
        self.assertEqual(registry.from_json(self.f, {"ratio": 0.25}), {"ratio": 0.25}, "a whole number is also a number")
        self.assertEqual(registry.from_json(self.f, None), {})

    def test_wrong_types_are_refused_with_the_name_and_what_was_wanted(self):
        for bad, words in [({"text": 5}, "'text' must be text"), ({"text": None}, "'text' must be text"),
                           ({"count": True}, "'count' must be a whole number"), ({"count": 1.5}, "'count' must be a whole number"),
                           ({"count": "1"}, "'count' must be a whole number"), ({"ratio": True}, "'ratio' must be a number"),
                           ({"ratio": float("nan")}, "'ratio' must be a number"), ({"ratio": "x"}, "'ratio' must be a number"),
                           ({"on": 1}, "'on' must be true or false"), ({"on": "true"}, "'on' must be true or false")]:
            with self.assertRaisesRegex(FeatureError, words, msg=bad):
                registry.from_json(self.f, bad)

    def test_an_unknown_name_or_a_non_object_is_refused(self):
        with self.assertRaisesRegex(FeatureError, "no parameter colour"):
            registry.from_json(self.f, {"colour": "red"})
        for bad in ([], "text", 5):
            with self.assertRaisesRegex(FeatureError, "must be an object"):
                registry.from_json(self.f, bad)

    def test_long_text_is_refused_for_every_front_end(self):
        registry.call("typed", text="a" * registry.MAX_STRING)
        with self.assertRaisesRegex(FeatureError, "too long"):
            registry.call("typed", text="a" * (registry.MAX_STRING + 1))


class TheAdapterFindsSecrets(Isolated):
    def test_the_secrets_in_env_are_found_and_the_rest_are_not(self):
        self.env("MQTT_PASSWORD=aaaaaaaaaaaa\nGRAFANA_ADMIN_PASSWORD=bbbbbbbbbbbb\nCHECKIN_KEY=cccccccccccc\n"
                 "CHECKIN_PORT=8090\nCOMPOSE_PROFILES=mqtt\nMQTT_USER=fleet\n")
        found = fleetlink.secret_values()
        self.assertTrue({"aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"} <= found)
        for plain in ("8090", "mqtt", "fleet"):
            self.assertNotIn(plain, found)

    def test_the_api_token_counts_as_a_secret(self):
        os.environ["FLEET_API_TOKEN"] = "t" * 20
        self.assertIn("t" * 20, fleetlink.secret_values())

    def test_a_broken_env_file_does_not_break_the_guard(self):
        self.env("this is not = a valid\x00 file")
        fleetlink.secret_values()  # must not raise


# ----------------------------------------------------------------------------- the features
class WithStack(Isolated, WithFakeVM):
    def setUp(self):
        WithFakeVM.setUp(self)
        Isolated.setUp(self)

    def call(self, name, **kwargs):
        return registry.call(name, **kwargs)


class ListingDevices(WithStack):
    def test_devices_are_shown_with_units_and_nulls(self):
        self.devices(**{"kitchen-pi": dev(age=3, cpu=12.5, temp=51.0),
                        "garage": dev(age=4, job="checkin", cpu=None, mem=None, disk=None, temp=None, uptime=300)})
        out = self.call("list_devices")
        self.assertEqual((out["count"], out["online"], out["offline"]), (2, 2, 0))
        pi = next(d for d in out["devices"] if d["device_id"] == "kitchen-pi")
        self.assertEqual((pi["state"], pi["reports_via"], pi["cpu_percent"], pi["temperature_celsius"]),
                         ("ONLINE", "agent", 12.5, 51.0))
        self.assertEqual(pi["offline_after_seconds"], 30)
        self.assertIsInstance(pi["offline_after_seconds"], float, "always a float, so the type never varies")
        self.assertRegex(pi["last_seen"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        garage = next(d for d in out["devices"] if d["device_id"] == "garage")
        self.assertEqual(garage["reports_via"], "checkin")
        self.assertIsNone(garage["cpu_percent"])

    def test_the_state_filter_and_counts(self):
        self.devices(up=dev(age=3), down=dev(age=500))
        both = self.call("list_devices")
        self.assertEqual((both["online"], both["offline"]), (1, 1))
        self.assertEqual([d["device_id"] for d in self.call("list_devices", state="offline")["devices"]], ["down"])
        only = self.call("list_devices", state="online")
        self.assertEqual((only["count"], only["online"], only["offline"]), (1, 1, 1), "the totals stay about all devices")
        with self.assertRaisesRegex(FeatureError, "state must be one of"):
            self.call("list_devices", state="sleeping")

    def test_a_slow_reporter_is_judged_by_its_own_limit(self):
        self.devices(slow=dev(age=100, interval=60.0), fast=dev(age=100))
        got = {d["device_id"]: d for d in self.call("list_devices")["devices"]}
        self.assertEqual((got["slow"]["state"], got["slow"]["offline_after_seconds"]), ("ONLINE", 180))
        self.assertEqual(got["fast"]["state"], "OFFLINE")

    def test_nan_becomes_null_so_the_answer_stays_valid_json(self):
        self.devices(odd=dev(cpu=float("nan"), temp=float("inf")))
        d = self.call("list_devices")["devices"][0]
        self.assertIsNone(d["cpu_percent"])
        self.assertIsNone(d["temperature_celsius"])

    def test_no_devices_says_what_to_do_and_a_stopped_stack_is_a_503(self):
        self.assertIn("how_to_add_device", self.call("list_devices")["note"])
        FakeVM.state["healthy"] = False
        with self.assertRaises(FeatureError) as cm:
            self.call("list_devices")
        self.assertEqual(cm.exception.code, 503)
        self.assertIn("./fleet up", str(cm.exception))


class OneDevice(WithStack):
    def test_an_online_device_is_explained(self):
        self.devices(pi=dev(age=3))
        out = self.call("get_device", device_id="pi")
        self.assertTrue(out["found"])
        self.assertEqual(out["state"], "ONLINE")
        self.assertIn("OFFLINE after 30 s", out["explanation"])
        self.assertEqual(out["next_steps"], [])

    def test_an_offline_device_says_why_and_what_to_try_for_each_way_of_reporting(self):
        for job, word in [("device-agent", "agent"), ("mqtt-bridge", "mqtt-bridge"), ("checkin", "check-in URL"),
                          ("watcher", "fleet watch list")]:
            self.devices(box=dev(age=400, interval=60.0, job=job))
            out = self.call("get_device", device_id="box")
            self.assertEqual(out["state"], "OFFLINE")
            self.assertIn("3 x the 1 min it reports every", out["explanation"])
            self.assertIn("limit for this device is 3 min", out["explanation"])
            self.assertIn(word, " ".join(out["next_steps"]), job)
            self.assertIn("cannot do that", out["next_steps"][-1], "removal is for a person")

    def test_a_device_that_has_not_declared_an_interval_is_held_to_the_floor(self):
        self.devices(box=dev(age=400))
        self.assertIn("has not said how often it reports", self.call("get_device", device_id="box")["explanation"])

    def test_an_unknown_device_is_an_answer_not_an_error(self):
        self.devices(alpha=dev(), beta=dev())
        out = self.call("get_device", device_id="alpah")
        self.assertFalse(out["found"])
        self.assertEqual(out["known_devices"], ["alpha", "beta"])
        self.assertIn("15 seconds", out["explanation"])


class WaitingDevices(WithStack):
    """A device you registered with ./fleet add that has not reported yet (a brand-new board) is WAITING, not invisible."""

    def expect(self, **devices):
        fleet.write_expected({d: {"kind": kind, "added": int(__import__("time").time()) - 125} for d, kind in devices.items()})

    def test_it_is_listed_with_the_same_keys_and_nothing_to_read(self):
        self.expect(**{"new-board": "mqtt"})
        self.devices(up=dev(age=2))
        out = self.call("list_devices")
        self.assertEqual((out["count"], out["online"], out["offline"], out["waiting"]), (2, 1, 0, 1))
        board = next(d for d in out["devices"] if d["device_id"] == "new-board")
        up = next(d for d in out["devices"] if d["device_id"] == "up")
        self.assertEqual(set(board), set(up), "the same keys for every device, whatever its state")
        self.assertEqual((board["state"], board["reports_via"], board["cpu_percent"], board["last_seen"]),
                         ("WAITING", "mqtt", None, None))
        self.assertRegex(board["registered_at"], r"^\d{4}-\d\d-\d\dT")
        self.assertNotIn("note", out)

    def test_the_filter_and_the_counts(self):
        self.expect(**{"new-board": "agent"})
        self.devices(up=dev(age=2), down=dev(age=900))
        self.assertEqual([d["device_id"] for d in self.call("list_devices", state="waiting")["devices"]], ["new-board"])
        for state, ids in (("online", ["up"]), ("offline", ["down"])):
            out = self.call("list_devices", state=state)
            self.assertEqual([d["device_id"] for d in out["devices"]], ids)
            self.assertEqual(out["waiting"], 1, "the totals stay about everything")

    def test_a_device_that_reports_stops_waiting(self):
        self.expect(**{"board": "agent"})
        self.assertEqual(self.call("list_devices")["waiting"], 1)
        self.devices(board=dev(age=2))
        out = self.call("list_devices")
        self.assertEqual((out["waiting"], out["online"], out["devices"][0]["state"]), (0, 1, "ONLINE"))

    def test_a_watched_target_that_never_answered_is_waiting_too(self):
        fleet.write_targets([{"id": "router", "kind": "ping", "target": "10.0.0.1"}])
        out = self.call("list_devices", state="waiting")
        self.assertEqual([(d["device_id"], d["reports_via"], d["registered_at"]) for d in out["devices"]],
                         [("router", "watcher", None)])
        text = self.call("get_device", device_id="router")["explanation"]
        self.assertIn("Being watched", text)

    def test_get_device_explains_what_finishes_the_setup_for_each_way(self):
        for kind, words in (("agent", "port 4317"), ("mqtt", "firmware=micropython"), ("mqtt", "uptime_seconds"),
                            ("checkin", "check-in curl"), ("watch", "fleet watch list")):
            self.expect(**{"board": kind})
            out = self.call("get_device", device_id="board")
            self.assertTrue(out["found"])
            self.assertEqual(out["state"], "WAITING")
            self.assertIn("Registered 2 min", out["explanation"])
            self.assertIn("No alert fires for a device that has never reported", out["explanation"])
            self.assertIn(words, " ".join(out["next_steps"]), kind)
            self.assertIn("cannot do that", out["next_steps"][-1])

    def test_an_unknown_id_still_is_not_found_and_the_known_list_includes_waiting_ones(self):
        self.expect(**{"new-board": "agent"})
        self.devices(up=dev())
        out = self.call("get_device", device_id="nope")
        self.assertFalse(out["found"])
        self.assertEqual(out["known_devices"], ["new-board", "up"])
        self.assertIn("none is registered", out["explanation"])

    def test_an_unreadable_list_is_a_warning_and_the_rest_still_answers(self):
        with open(fleet.EXPECTED_FILE, "w") as f:
            f.write("nonsense")
        self.devices(up=dev(age=2))
        out = self.call("list_devices")
        self.assertEqual((out["count"], out["waiting"]), (1, 0))
        self.assertIn("could not be read", out["warning"])
        self.assertTrue(self.call("get_device", device_id="up")["found"])

    def test_status_counts_them(self):
        self.expect(**{"a": "agent", "b": "mqtt"})
        self.devices(up=dev(age=2))
        self.assertEqual(self.call("stack_status")["devices"], {"online": 1, "offline": 0, "waiting": 2})


class StackStatus(WithStack):
    def test_a_healthy_stack_with_inputs(self):
        self.env("COMPOSE_PROFILES=mqtt,checkin\nCHECKIN_PORT=8090\nMQTT_PASSWORD=" + SECRET + "\n")
        self.devices(a=dev(age=3), b=dev(age=900))
        with mock.patch.object(fleet, "port_open", lambda h, p, timeout=2: True), \
             mock.patch.object(fleet, "grafana_healthy", lambda: True), \
             mock.patch.object(fleet, "dashboard_state", lambda: "ok"), \
             mock.patch.object(fleet, "notify_configured", lambda: True):
            out = self.call("stack_status")
        self.assertTrue(out["running"])
        self.assertEqual(out["devices"], {"online": 1, "offline": 1, "waiting": 0})
        self.assertEqual(out["inputs"]["mqtt"]["broker"], "bundled")
        self.assertEqual((out["inputs"]["checkin_url"]["on"], out["inputs"]["checkin_url"]["port"]), (True, 8090))
        self.assertEqual(out["inputs"]["device_agent"]["endpoint"], "192.168.1.10:4317")
        self.assertEqual(out["dashboard"]["url"], "http://192.168.1.10:3000")
        self.assertTrue(out["alerts_go_to_a_notification_channel"])
        self.assertNotIn("next_step", out)
        self.assertNotIn(SECRET, json.dumps(out))

    def test_a_partly_stopped_stack_names_what_is_down(self):
        out = self.call("stack_status")  # the fake VM answers; Grafana and the collector do not
        self.assertFalse(out["running"])
        self.assertEqual(out["services"], {"victoriametrics": True, "grafana": False, "otel-collector": False})
        self.assertIn("grafana, otel-collector", out["next_step"])
        self.assertIn("./fleet up", out["next_step"])
        self.assertIsNone(out["dashboard"]["state"])

    def test_a_stack_that_is_completely_down_still_answers(self):
        FakeVM.state["healthy"] = False
        out = self.call("stack_status")
        self.assertFalse(out["running"])
        self.assertIsNone(out["devices"])

    def test_off_inputs_say_how_to_turn_them_on(self):
        out = self.call("stack_status")
        self.assertFalse(out["inputs"]["mqtt"]["on"])
        self.assertIn("./fleet up --mqtt", out["inputs"]["mqtt"]["turn_on"])
        self.assertFalse(out["inputs"]["checkin_url"]["on"])
        self.assertIn("--checkin", out["inputs"]["checkin_url"]["turn_on"])


class EveryFeature(WithStack):
    def test_each_real_feature_runs_and_returns_json_with_the_stack_up_and_down(self):
        self.assertEqual(set(REAL_FEATURES), {"stack_status", "list_devices", "get_device", "how_to_add_device"})
        self.devices(a=dev())
        for healthy in (True, False):
            FakeVM.state["healthy"] = healthy
            for name in REAL_FEATURES:
                args = {"device_id": "a"} if name == "get_device" else {}
                try:
                    out = self.call(name, **args)
                except FeatureError as exc:  # a stopped stack is an explained error, never a crash
                    self.assertFalse(healthy, name)
                    self.assertEqual(exc.code, 503)
                    continue
                self.assertIsInstance(out, dict, name)
                json.dumps(out, allow_nan=False)


class AddingADevice(WithStack):
    def test_the_overview_lists_the_four_ways(self):
        out = self.call("how_to_add_device")
        self.assertEqual(set(out["ways"]), {"agent", "mqtt", "checkin", "watch"})
        self.assertIn("kind", out["next"])

    def test_each_way_has_steps_that_end_by_checking_it_arrived(self):
        for kind in ("agent", "mqtt", "checkin", "watch"):
            out = self.call("how_to_add_device", kind=kind)
            self.assertTrue(out["steps"], kind)
            self.assertEqual({s["where"] for s in out["steps"]} - {"server", "device", "board", "anywhere"}, set())
            self.assertIn("get_device", out["steps"][-1]["action"], kind)

    def test_the_device_id_and_address_are_filled_in(self):
        out = self.call("how_to_add_device", kind="agent", device_id="kitchen-pi")
        commands = " ".join(s.get("command", "") for s in out["steps"])
        self.assertIn("./fleet add kitchen-pi", commands)
        self.assertIn("--device-id kitchen-pi --endpoint 192.168.1.10:4317", commands)
        self.assertNotIn("<", commands)

    def test_placeholders_stay_when_nothing_is_known(self):
        self.env("CHECKIN_PORT=8090\n")
        with mock.patch.object(fleet, "lan_ip", lambda: None):
            out = self.call("how_to_add_device", kind="checkin")
        curl = next(s["command"] for s in out["steps"] if s.get("command", "").startswith("curl"))
        self.assertIn("http://SERVER-ADDRESS:8090/checkin/<device_id>?key=<CHECKIN_KEY>", curl)

    def test_a_bad_id_or_kind_is_refused(self):
        with self.assertRaisesRegex(FeatureError, "lowercase letters"):
            self.call("how_to_add_device", kind="agent", device_id="Bad ID")
        with self.assertRaisesRegex(FeatureError, "kind must be one of: agent, mqtt, checkin, watch"):
            self.call("how_to_add_device", kind="usb")

    def test_no_password_or_key_is_ever_shown(self):
        self.env(f"MQTT_PASSWORD={SECRET}\nCHECKIN_KEY={SECRET}x\nGRAFANA_ADMIN_PASSWORD={SECRET}y\n")
        for kind in ("", "agent", "mqtt", "checkin", "watch"):
            text = json.dumps(self.call("how_to_add_device", kind=kind, device_id="x"))
            self.assertNotIn(SECRET, text, kind)
        self.assertIn("<CHECKIN_KEY>", json.dumps(self.call("how_to_add_device", kind="checkin")))

    def test_the_mqtt_and_checkin_state_is_reported(self):
        self.env("COMPOSE_PROFILES=mqtt\n")
        self.assertTrue(self.call("how_to_add_device", kind="mqtt")["mqtt_is_on"])
        self.assertFalse(self.call("how_to_add_device", kind="checkin")["checkin_is_on"])


class TheGuidanceMatchesTheRealCommands(unittest.TestCase):
    """The guidance is text; this keeps it from drifting away from ./fleet and the files it names."""

    def test_every_fleet_command_in_it_is_a_command_fleet_accepts(self):
        from interface.features import onboarding
        parser = fleet.build_parser()
        seen = 0
        for kind, guide in onboarding.KINDS.items():
            for step in guide["steps"]:
                command = step.get("command", "")
                if not command.startswith("./fleet "):
                    continue
                argv = shlex.split(command.replace("<device_id>", "x").replace("<address>", "10.0.0.1"))[1:]
                try:
                    parser.parse_args(argv)
                except SystemExit:
                    self.fail(f"{kind}: './fleet' does not accept: {command}")
                seen += 1
        self.assertGreaterEqual(seen, 5)

    def test_the_files_it_names_exist(self):
        from interface.features import onboarding
        for kind, guide in onboarding.KINDS.items():
            for path in re.findall(r"(?:docs|agent)/[\w.-]+|README\.md", guide["docs"] + " " + " ".join(
                    s.get("command", "") for s in guide["steps"])):
                self.assertTrue(os.path.exists(os.path.join(ROOT, path)), f"{kind}: {path} does not exist")


# ----------------------------------------------------------------------------- the HTTP API
class Api(WithStack):
    token = None
    host = "127.0.0.1"

    def setUp(self):
        super().setUp()
        err = mock.patch("sys.stderr", new_callable=io.StringIO)  # the server's log; some tests read it
        self.log = err.start()
        self.addCleanup(err.stop)
        self.server = http_api.make_server(self.host, 0, self.token)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))
        self.port = self.server.server_address[1]

    def request(self, path, method="GET", headers=None, host_header=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True)
            conn.putheader("Host", host_header or f"localhost:{self.port}")
            for k, v in (headers or {}).items():
                conn.putheader(k, v)
            conn.endheaders()
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, dict(resp.getheaders()), (json.loads(raw) if raw else None)
        finally:
            conn.close()


class TheApiAnswers(Api):
    def test_the_index_lists_every_feature_with_its_parameters(self):
        status, headers, body = self.request("/v1")
        self.assertEqual(status, 200)
        self.assertTrue(body["read_only"])
        names = {f["name"]: f for f in body["features"]}
        self.assertEqual(set(names), set(REAL_FEATURES))
        self.assertEqual(names["get_device"]["parameters"], [{"name": "device_id", "type": "str", "required": True}])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertFalse([h for h in headers if h.lower().startswith("access-control")], "no CORS headers: pages cannot read it")

    def test_the_root_points_to_the_index(self):
        status, _, body = self.request("/")
        self.assertEqual((status, body["questions"]), (200, "/v1"))

    def test_a_feature_is_answered_and_filters_work(self):
        self.devices(up=dev(age=2), down=dev(age=900))
        _, _, everything = self.request("/v1/list_devices")
        self.assertEqual(everything["count"], 2)
        _, _, offline = self.request("/v1/list_devices?state=offline")
        self.assertEqual([d["device_id"] for d in offline["devices"]], ["down"])
        _, _, one = self.request("/v1/get_device?device_id=up")
        self.assertEqual((one["found"], one["state"]), (True, "ONLINE"))

    def test_a_trailing_slash_is_fine(self):
        self.assertEqual(self.request("/v1/")[0], 200)
        self.assertEqual(self.request("/v1/stack_status/")[0], 200)

    def test_bad_requests_are_400_with_a_reason(self):
        for path, words in [("/v1/list_devices?state=sleeping", "state must be one of"),
                            ("/v1/list_devices?colour=red", "no parameter colour"),
                            ("/v1/get_device", "needs: device_id"),
                            ("/v1/list_devices?state=all&state=online", "given twice"),
                            ("/v1/how_to_add_device?kind=usb", "kind must be one of")]:
            status, _, body = self.request(path)
            self.assertEqual(status, 400, path)
            self.assertIn(words, body["error"], path)

    def test_unknown_addresses_are_404(self):
        for path in ("/v1/no_such_feature", "/nothing", "/v2/list_devices"):
            self.assertEqual(self.request(path)[0], 404, path)

    def test_a_stopped_stack_is_503_with_the_fix(self):
        FakeVM.state["healthy"] = False
        status, _, body = self.request("/v1/list_devices")
        self.assertEqual(status, 503)
        self.assertIn("./fleet up", body["error"])

    def test_it_only_reads(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
            status, headers, _ = self.request("/v1/list_devices", method=method)
            self.assertEqual(status, 405, method)
            self.assertEqual(headers["Allow"], "GET")

    def test_a_very_long_request_is_refused(self):
        self.assertEqual(self.request("/v1/get_device?device_id=" + "a" * 3000)[0], 414)

    def test_long_text_is_a_400(self):
        status, _, body = self.request("/v1/get_device?device_id=" + "a" * 300)
        self.assertEqual(status, 400)
        self.assertIn("too long", body["error"])

    def test_too_many_parameters_are_a_400_not_a_crash(self):
        self.assertEqual(self.request("/v1/list_devices?" + "&".join(f"p{i}=1" for i in range(40)))[0], 400)


class TheApiNeverLeaks(Api):
    def test_a_bug_shows_no_details(self):
        with mock.patch.object(registry, "call", side_effect=RuntimeError("boom with /secret/path")):
            status, _, body = self.request("/v1/list_devices")
        self.assertEqual(status, 500)
        self.assertNotIn("boom", json.dumps(body))
        self.assertNotIn("/secret/path", json.dumps(body))
        self.assertIn("boom", self.log.getvalue(), "the details go to the server's own log")

    def test_a_secret_in_an_answer_is_stopped_before_it_leaves(self):
        self.env(f"MQTT_PASSWORD={SECRET}\n")
        self.devices(**{"x": dev(host=SECRET)})  # a bug, or an unlucky hostname, would put it in an answer
        status, _, body = self.request("/v1/list_devices")
        self.assertEqual(status, 500)
        self.assertNotIn(SECRET, json.dumps(body))
        self.assertNotIn(SECRET, json.dumps(self.request("/v1/get_device?device_id=x")[2]))
        self.assertIn("SecretLeak", self.log.getvalue())
        self.assertNotIn(SECRET, self.log.getvalue(), "nor in the log")


class TheApiLog(Api):
    token = "log-token-" + "z" * 20

    def test_the_log_has_no_query_strings_and_no_token(self):
        auth = {"Authorization": f"Bearer {self.token}"}
        self.devices(**{"my-device": dev()})
        self.request(f"/v1/list_devices?token={self.token}")                    # a mistake: the token in the address
        self.request("/v1/get_device?device_id=my-device", headers=auth)         # a normal call
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)    # a malformed line the base class quotes whole
        conn.sock = socket.create_connection(("127.0.0.1", self.port), 5)
        conn.sock.sendall(f"GET?token={self.token}\r\n\r\n".encode())  # one word: the base class quotes the whole line
        conn.sock.recv(4096)
        conn.close()
        log = self.log.getvalue()
        self.assertNotIn(self.token, log)
        self.assertNotIn("my-device", log, "device ids in a query string are not logged either")
        self.assertIn('"GET /v1/get_device" 200', log, "but the method, path and status are")
        self.assertIn('"GET /v1/list_devices" 401', log)
        self.assertIn("Bad request syntax", log)
        self.assertIn("<token>", log, "the quoted bad request line had the token scrubbed")
        self.assertNotIn("Traceback", log, "a request too broken to parse is logged without a crash")


class OnTheLocalMachineOnly(Api):
    def test_another_host_name_is_refused(self):
        """A web page can send a browser to this port under its own name (DNS rebinding); the Host header gives it away."""
        for host in ("evil.example.com", f"evil.example.com:{self.port}", "10.0.0.5:8765", "localhost.evil.com"):
            self.assertEqual(self.request("/v1", host_header=host)[0], 403, host)
        self.assertEqual(self.request("/v1", host_header=f"127.0.0.1:{self.port}")[0], 200)
        self.assertEqual(self.request("/v1", host_header="localhost")[0], 200)
        self.assertEqual(self.request("/v1", host_header=f"[::1]:{self.port}")[0], 200)


class TheTokenRules(unittest.TestCase):
    def test_listening_beyond_this_machine_needs_a_token(self):
        for host in ("0.0.0.0", "192.168.1.10", "::"):
            self.assertIn("FLEET_API_TOKEN", http_api.check_config(host, None) or "", host)
            self.assertIn("FLEET_API_TOKEN", http_api.check_config(host, "") or "", host)
            self.assertIsNone(http_api.check_config(host, "x" * 16), host)
        for host in ("127.0.0.1", "localhost", "::1", "127.0.0.5"):
            self.assertIsNone(http_api.check_config(host, None), host)

    def test_a_short_token_is_refused_everywhere(self):
        self.assertIn("at least 16", http_api.check_config("127.0.0.1", "short"))

    def test_make_server_refuses_instead_of_listening(self):
        with self.assertRaises(SystemExit) as cm:
            http_api.make_server("0.0.0.0", 0, None)
        self.assertIn("refusing to listen", str(cm.exception))


class WithAToken(Api):
    token = "t0ken-" + "x" * 20

    def test_without_the_right_token_nothing_is_answered(self):
        for headers in (None, {"Authorization": "Bearer wrong"}, {"Authorization": self.token},
                        {"Authorization": "Basic " + self.token}):
            status, response_headers, body = self.request("/v1/list_devices", headers=headers)
            self.assertEqual(status, 401, headers)
            self.assertEqual(response_headers["WWW-Authenticate"], "Bearer")
            self.assertNotIn("devices", body)
        self.assertEqual(self.request("/v1")[0], 401, "the index is protected too")

    def test_the_token_in_the_address_is_not_accepted(self):
        self.assertEqual(self.request(f"/v1/list_devices?token={self.token}")[0], 401)

    def test_the_right_token_works(self):
        self.devices(a=dev())
        status, _, body = self.request("/v1/list_devices", headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual((status, body["count"]), (200, 1))


class ListeningOnTheNetworkWithAToken(Api):
    host = "0.0.0.0"
    token = "net-" + "y" * 20

    def test_any_host_name_works_but_the_token_is_still_required(self):
        self.assertEqual(self.request("/v1", host_header="pi.local:8765")[0], 401)
        self.assertEqual(self.request("/v1", host_header="pi.local:8765",
                                      headers={"Authorization": f"Bearer {self.token}"})[0], 200)


@unittest.skipUnless(socket.has_ipv6, "no IPv6 here")
class OnIPv6Loopback(Api):
    host = "::1"

    def setUp(self):
        try:
            super().setUp()
        except OSError:
            self.skipTest("cannot bind ::1 here")

    def test_it_answers(self):
        conn = http.client.HTTPConnection("::1", self.port, timeout=5)
        conn.putrequest("GET", "/v1", skip_host=True)
        conn.putheader("Host", f"[::1]:{self.port}")
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()


if __name__ == "__main__":
    unittest.main()
