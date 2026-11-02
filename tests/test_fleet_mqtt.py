"""Tests for the MQTT parts of the `fleet` command: the .env settings, `up --mqtt`, `add --mqtt`, `down`.
Stdlib only. No Docker and no broker: subprocess calls are patched and a fake VictoriaMetrics answers queries.
The live path (a real Mosquitto and bridge) is covered by scripts/validate_mqtt.sh.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import contextlib
import io
import os
import re
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_fleet import FakeVM, WithFakeVM, dev, fleet, run  # noqa: E402


class WithEnv(unittest.TestCase):
    """A private .env for each test."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env_path = os.path.join(self.dir.name, ".env")
        p = mock.patch.object(fleet, "ENV_FILE", self.env_path)
        p.start()
        self.addCleanup(p.stop)
        self.calls = []

        def fake_run(cmd, **kw):
            self.calls.append(cmd)
            return mock.Mock(returncode=0, stdout="", stderr="")

        p = mock.patch.object(fleet.subprocess, "run", fake_run)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(lambda: os.environ.pop("FLEET_MQTT_PASSWORD", None))

    def write(self, text):
        with open(self.env_path, "w") as f:
            f.write(text)

    def read(self):
        with open(self.env_path) as f:
            return f.read()

    def parse(self, *argv):
        return fleet.build_parser().parse_args(["up", *argv])


class EnvFileTests(WithEnv):
    def test_missing_file_reads_as_empty(self):
        self.assertEqual(fleet.read_env(), {})
        self.assertIsNone(fleet.mqtt_mode())

    def test_write_keeps_other_lines_and_changes_only_the_given_keys(self):
        self.write("# mine\nFOO=bar\nMQTT_USERNAME=old\n\nBAZ='a b'\n")
        fleet.write_env({"MQTT_USERNAME": "new", "MQTT_PASSWORD": "pw"})
        text = self.read()
        self.assertIn("# mine\nFOO=bar\nMQTT_USERNAME=new\n\nBAZ='a b'\n", text)
        self.assertTrue(text.endswith("MQTT_PASSWORD=pw\n"))
        self.assertEqual(text.count("MQTT_USERNAME"), 1)

    def test_none_removes_a_key(self):
        self.write("A=1\nB=2\n")
        fleet.write_env({"A": None, "C": None})
        self.assertEqual(self.read(), "B=2\n")

    def test_the_file_is_private_to_the_owner(self):
        fleet.write_env({"MQTT_PASSWORD": "secret"})
        self.assertEqual(stat.S_IMODE(os.stat(self.env_path).st_mode), 0o600)
        self.assertFalse(os.path.exists(self.env_path + ".tmp"))

    def test_values_that_compose_would_misread_are_quoted_or_refused(self):
        fleet.write_env({"A": "has space", "B": "dollar$sign", "C": "plain-1.2_3"})
        self.assertEqual(fleet.read_env(), {"A": "has space", "B": "dollar$sign", "C": "plain-1.2_3"})
        self.assertIn("B='dollar$sign'", self.read())  # single quotes: taken literally by Docker Compose
        for bad in ["it's", "line\nbreak", "cr\rhere", "nul\0"]:
            with self.assertRaises(fleet.FleetError, msg=repr(bad)):
                fleet.write_env({"X": bad})

    def test_mode_and_other_profiles(self):
        for profiles, mode, others in [("", None, []), ("mqtt", "bundled", []), ("mqtt-bridge", "external", []),
                                       ("legacy-vector", None, ["legacy-vector"]),
                                       ("legacy-vector,mqtt", "bundled", ["legacy-vector"])]:
            env = {"COMPOSE_PROFILES": profiles}
            self.assertEqual((fleet.mqtt_mode(env), fleet.other_profiles(env)), (mode, others), profiles)


class BrokerAddressTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(fleet.parse_broker("192.168.1.20"), ("192.168.1.20", 1883))
        self.assertEqual(fleet.parse_broker("broker.lan:8883"), ("broker.lan", 8883))
        self.assertEqual(fleet.parse_broker(" mq-1 "), ("mq-1", 1883))

    def test_invalid(self):
        for bad in ["", "a b", "host:", "host:abc", "host:0", "host:65536", "-lead", "trail-", "ho st:1",
                    "[::1]:1883", "fe80::1", "http://host", "host/path", "a;b", "$(id)"]:
            with self.assertRaises(fleet.FleetError, msg=bad):
                fleet.parse_broker(bad)

    def test_loopback_is_refused_with_the_reason(self):
        for bad in ["localhost", "LOCALHOST:1883", "127.0.0.1", "127.1.2.3:1883", "0.0.0.0"]:
            with self.assertRaises(fleet.FleetError, msg=bad) as cm:
                fleet.parse_broker(bad)
            self.assertIn("container", str(cm.exception))


class ApplyArgsTests(WithEnv):
    def test_nothing_asked_changes_nothing(self):
        self.assertFalse(fleet.apply_mqtt_args(self.parse()))
        self.assertFalse(os.path.exists(self.env_path))

    def test_bundled_writes_profile_user_and_a_random_password(self):
        self.assertTrue(fleet.apply_mqtt_args(self.parse("--mqtt")))
        env = fleet.read_env()
        self.assertEqual((env["COMPOSE_PROFILES"], env["MQTT_USERNAME"]), ("mqtt", "fleet"))
        self.assertRegex(env["MQTT_PASSWORD"], r"^[0-9a-f]{24}$")
        self.assertEqual(fleet.mqtt_mode(), "bundled")
        self.assertNotIn("MQTT_HOST", env)

    def test_asking_again_keeps_the_password_devices_already_use(self):
        fleet.apply_mqtt_args(self.parse("--mqtt"))
        first = fleet.read_env()["MQTT_PASSWORD"]
        fleet.apply_mqtt_args(self.parse("--mqtt"))
        self.assertEqual(fleet.read_env()["MQTT_PASSWORD"], first)

    def test_other_profiles_survive(self):
        self.write("COMPOSE_PROFILES=legacy-vector\n")
        fleet.apply_mqtt_args(self.parse("--mqtt"))
        self.assertEqual(fleet.read_env()["COMPOSE_PROFILES"], "legacy-vector,mqtt")
        self.calls.clear()
        fleet.apply_mqtt_args(self.parse("--no-mqtt"))
        self.assertEqual(fleet.read_env()["COMPOSE_PROFILES"], "legacy-vector")

    def test_external_broker_with_a_password_from_the_environment(self):
        os.environ["FLEET_MQTT_PASSWORD"] = "p@ss w0rd"
        fleet.apply_mqtt_args(self.parse("--mqtt-broker", "192.168.1.20:1884", "--mqtt-user", "ha"))
        env = fleet.read_env()
        self.assertEqual((fleet.mqtt_mode(), env["MQTT_HOST"], env["MQTT_PORT"], env["MQTT_USERNAME"], env["MQTT_PASSWORD"]),
                         ("external", "192.168.1.20", "1884", "ha", "p@ss w0rd"))
        self.assertNotIn("p@ss", " ".join(map(str, self.calls)))  # never on a command line

    def test_external_broker_without_a_password_stops_before_writing_anything(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            with self.assertRaises(fleet.FleetError) as cm:
                fleet.apply_mqtt_args(self.parse("--mqtt-broker", "192.168.1.20", "--mqtt-user", "ha"))
        self.assertIn("FLEET_MQTT_PASSWORD", str(cm.exception))
        self.assertFalse(os.path.exists(self.env_path))

    def test_external_broker_password_prompt_is_hidden(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch.object(fleet.getpass, "getpass", return_value="typed") as gp:
            fleet.apply_mqtt_args(self.parse("--mqtt-broker", "broker.lan", "--mqtt-user", "ha"))
        gp.assert_called_once()
        self.assertEqual(fleet.read_env()["MQTT_PASSWORD"], "typed")

    def test_an_anonymous_external_broker_needs_no_password(self):
        fleet.apply_mqtt_args(self.parse("--mqtt-broker", "broker.lan"))
        env = fleet.read_env()
        self.assertEqual((env["MQTT_USERNAME"], env["MQTT_PASSWORD"]), ("", ""))

    def test_there_is_no_password_flag(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            fleet.build_parser().parse_args(["up", "--mqtt-broker", "h", "--mqtt-password", "x"])

    def test_user_without_a_broker_is_an_error(self):
        with self.assertRaises(fleet.FleetError):
            fleet.apply_mqtt_args(self.parse("--mqtt", "--mqtt-user", "x"))

    def test_choices_exclude_each_other(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            fleet.build_parser().parse_args(["up", "--mqtt", "--no-mqtt"])

    def test_switching_from_an_external_broker_to_ours_does_not_reuse_its_password(self):
        os.environ["FLEET_MQTT_PASSWORD"] = "theirs"
        fleet.apply_mqtt_args(self.parse("--mqtt-broker", "broker.lan", "--mqtt-user", "ha"))
        fleet.apply_mqtt_args(self.parse("--mqtt"))
        env = fleet.read_env()
        self.assertNotEqual(env["MQTT_PASSWORD"], "theirs")
        self.assertEqual((env["MQTT_USERNAME"], "MQTT_HOST" in env), ("fleet", False))

    def test_switching_to_your_own_broker_stops_ours(self):
        fleet.apply_mqtt_args(self.parse("--mqtt"))
        self.calls.clear()
        os.environ["FLEET_MQTT_PASSWORD"] = "theirs"
        fleet.apply_mqtt_args(self.parse("--mqtt-broker", "broker.lan", "--mqtt-user", "ha"))
        self.assertEqual(self.calls, [["docker", "compose", "--profile", "*", "rm", "-sf", "mosquitto"]])
        self.calls.clear()  # but starting from "off" has nothing to stop
        fleet.apply_mqtt_args(self.parse("--no-mqtt"))
        self.calls.clear()
        fleet.apply_mqtt_args(self.parse("--mqtt-broker", "broker.lan", "--mqtt-user", "ha"))
        self.assertEqual(self.calls, [])

    def test_off_removes_every_setting_and_stops_the_containers(self):
        fleet.apply_mqtt_args(self.parse("--mqtt"))
        self.calls.clear()
        fleet.apply_mqtt_args(self.parse("--no-mqtt"))
        self.assertEqual(fleet.read_env(), {})
        self.assertEqual(self.calls, [["docker", "compose", "--profile", "*", "rm", "-sf", "mosquitto", "mqtt-bridge"]])


class UpTests(WithEnv):
    def setUp(self):
        super().setUp()
        for name, new in [("require_docker", lambda: None), ("missing_images", lambda: []),
                          ("compose_ps", lambda: {}), ("port_open", lambda *a, **k: False),
                          ("service_checks", lambda: {"victoriametrics": (True, "healthy")}),
                          ("dashboard_state", lambda: "ok"), ("lan_ip", lambda: "10.0.0.5"),
                          ("fetch_devices", lambda: {}), ("notify_configured", lambda: None)]:
            p = mock.patch.object(fleet, name, new)
            p.start()
            self.addCleanup(p.stop)

    def up_commands(self):
        return [c for c in self.calls if c[:4] == ["docker", "compose", "up", "-d"]]

    def test_plain_up_does_not_touch_mqtt(self):
        code, out, _ = run(["up"])
        self.assertEqual(code, 0)
        self.assertEqual(self.up_commands(), [["docker", "compose", "up", "-d"]])
        self.assertNotIn("MQTT", out)
        self.assertFalse(os.path.exists(self.env_path))

    def test_mqtt_builds_the_bridge_and_prints_where_the_broker_is(self):
        code, out, _ = run(["up", "--mqtt"])
        self.assertEqual(code, 0)
        self.assertEqual(self.up_commands(), [["docker", "compose", "up", "-d", "--build"]])
        self.assertIn("10.0.0.5:1883", out)
        self.assertIn("./fleet add <id> --mqtt", out)
        self.assertNotIn(fleet.read_env()["MQTT_PASSWORD"], out)  # the password is shown by `add`, not on every start

    def test_the_choice_is_remembered(self):
        run(["up", "--mqtt"])
        self.calls.clear()
        run(["up"])
        self.assertEqual(self.up_commands(), [["docker", "compose", "up", "-d", "--build"]])

    def test_a_bad_broker_address_stops_before_anything_starts(self):
        code, _, err = run(["up", "--mqtt-broker", "localhost"])
        self.assertEqual(code, 1)
        self.assertIn("container", err)
        self.assertEqual(self.up_commands(), [])

    def test_a_busy_broker_port_is_explained_and_changes_nothing(self):
        with mock.patch.object(fleet, "port_open", lambda *a, **k: True):
            code, _, err = run(["up", "--mqtt"])
        self.assertEqual(code, 1)
        self.assertIn("port 1883 is already in use", err)
        self.assertIn("--mqtt-broker 10.0.0.5", err)  # the way out is named
        self.assertFalse(os.path.exists(self.env_path))  # .env untouched
        self.assertEqual(self.up_commands(), [])

    def test_our_own_running_broker_is_not_a_conflict(self):
        with mock.patch.object(fleet, "port_open", lambda *a, **k: True), \
                mock.patch.object(fleet, "compose_ps", lambda: {"mosquitto": "running"}):
            code, _, _ = run(["up", "--mqtt"])
        self.assertEqual(code, 0)

    def test_an_external_broker_on_that_port_is_fine(self):
        with mock.patch.object(fleet, "port_open", lambda *a, **k: True):
            code, _, _ = run(["up", "--mqtt-broker", "10.0.0.9"])
        self.assertEqual(code, 0)

    def test_a_refused_bridge_login_is_named_in_the_error(self):
        reason = "the broker at b:1884 refused the connection: Not authorized (check MQTT_USERNAME and MQTT_PASSWORD)"
        checks = {"victoriametrics": (True, "healthy"), "mqtt-bridge": (False, reason)}
        with mock.patch.object(fleet, "service_checks", lambda: checks):
            code, _, err = run(["up", "--mqtt", "--timeout", "0"])
        self.assertEqual(code, 1)
        self.assertIn("mqtt-bridge (the broker at b:1884 refused the connection: Not authorized", err)

    def test_a_bridge_that_is_just_starting_gets_no_made_up_reason(self):
        checks = {"victoriametrics": (True, "healthy"), "mqtt-bridge": (False, "starting")}
        with mock.patch.object(fleet, "service_checks", lambda: checks):
            _, _, err = run(["up", "--mqtt", "--timeout", "0"])
        self.assertIn("not ready after 0s: mqtt-bridge. See:", err)

    def test_off_says_so(self):
        run(["up", "--mqtt"])
        self.calls.clear()
        code, out, _ = run(["up", "--no-mqtt"])
        self.assertEqual(code, 0)
        self.assertEqual(self.up_commands(), [["docker", "compose", "up", "-d"]])
        self.assertIn("MQTT is off", out)


class ServiceCheckTests(WithEnv):
    def test_mqtt_services_are_checked_only_when_mqtt_is_on(self):
        with mock.patch.object(fleet, "vm_healthy", return_value=True), mock.patch.object(fleet, "grafana_healthy", return_value=True), \
                mock.patch.object(fleet, "port_open", return_value=True), mock.patch.object(fleet, "bridge_status", return_value=(True, "connected to x")):
            self.assertEqual(sorted(fleet.service_checks()), ["grafana", "otel-collector", "victoriametrics"])
            self.write("COMPOSE_PROFILES=mqtt\n")
            self.assertEqual(sorted(fleet.service_checks()),
                             ["grafana", "mosquitto", "mqtt-bridge", "otel-collector", "victoriametrics"])
            self.write("COMPOSE_PROFILES=mqtt-bridge\n")
            self.assertEqual(sorted(fleet.service_checks()),
                             ["grafana", "mqtt-bridge", "otel-collector", "victoriametrics"])

    def logs(self, text):
        return mock.patch.object(fleet, "compose", return_value=mock.Mock(returncode=0, stdout=text))

    def test_bridge_status_follows_the_latest_event(self):
        L = "2026-11-02 15:19:12,819 "
        cases = [
            ("", (False, "starting")),
            (L + "INFO starting: broker b:1883, VictoriaMetrics http://vm\n", (False, "starting")),
            (L + "INFO connected to b:1883; listening on fleet/+/+\n", (True, "connected to b:1883")),
            (L + "WARNING cannot reach the broker at b:1883; retrying every few seconds\n",
             (False, "cannot reach the broker at b:1883; retrying every few seconds")),
            (L + "INFO connected to b:1883; listening on fleet/+/+\n" + L + "WARNING lost the connection to the broker (x); reconnecting\n",
             (False, "lost the connection to the broker (x); reconnecting")),
            (L + "ERROR the broker at b:1883 refused the connection: Not authorized (check MQTT_USERNAME and MQTT_PASSWORD)\n",
             (False, "the broker at b:1883 refused the connection: Not authorized (check MQTT_USERNAME and MQTT_PASSWORD)")),
            # a restart begins a new life: an old "connected" must not count
            (L + "INFO connected to b:1883; listening on fleet/+/+\n" + L + "INFO stopped\n" + L + "INFO starting: broker b:1883, VictoriaMetrics v\n",
             (False, "starting")),
            ("not a log line\n" + L + "WARNING a device topic warning\n", (False, "starting")),
        ]
        for text, want in cases:
            with self.logs(text):
                self.assertEqual(fleet.bridge_status(), want, text)


class AddMqttTests(WithEnv, WithFakeVM):
    def setUp(self):
        WithFakeVM.setUp(self)
        WithEnv.setUp(self)

    def test_refuses_when_mqtt_is_off(self):
        code, _, err = run(["add", "garage", "--mqtt", "--no-wait"])
        self.assertEqual(code, 1)
        self.assertIn("./fleet up --mqtt", err)

    def test_bundled_broker_instructions_include_everything_a_board_needs(self):
        self.write("COMPOSE_PROFILES=mqtt\nMQTT_USERNAME=fleet\nMQTT_PASSWORD=abc123\n")
        code, out, _ = run(["add", "garage-esp32", "--mqtt", "--host", "10.0.0.5", "--no-wait"])
        self.assertEqual(code, 0)
        for text in ["10.0.0.5:1883", "fleet", "abc123", "fleet/garage-esp32/uptime_seconds", "REQUIRED",
                     "fleet/garage-esp32/report_interval_seconds", "fleet/garage-esp32/temperature_celsius",
                     "mosquitto_pub -h 10.0.0.5 -p 1883 -u fleet -P 'abc123' -t fleet/garage-esp32/uptime_seconds -m 1",
                     "docs/MQTT.md"]:
            self.assertIn(text, out)
        self.assertNotIn("can change", out)  # the hotspot warning only appears for an auto-detected address

    def test_external_broker_never_shows_a_password(self):
        self.write("COMPOSE_PROFILES=mqtt-bridge\nMQTT_HOST=broker.lan\nMQTT_PORT=1884\nMQTT_USERNAME=ha\nMQTT_PASSWORD=their-secret\n")
        code, out, _ = run(["add", "valve-1", "--mqtt", "--no-wait"])
        self.assertEqual(code, 0)
        self.assertIn("broker.lan:1884", out)
        self.assertIn("<your broker password>", out)
        self.assertNotIn("their-secret", out)

    def test_anonymous_broker_has_no_login_in_the_examples(self):
        self.write("COMPOSE_PROFILES=mqtt-bridge\nMQTT_HOST=broker.lan\nMQTT_PORT=1883\nMQTT_USERNAME=\nMQTT_PASSWORD=\n")
        _, out, _ = run(["add", "valve-1", "--mqtt", "--no-wait"])
        self.assertIn("mosquitto_pub -h broker.lan -p 1883 -t fleet/valve-1/", out)
        self.assertNotIn(" -u ", out)

    def test_refuses_an_id_that_is_already_reporting(self):
        self.write("COMPOSE_PROFILES=mqtt\nMQTT_PASSWORD=x\n")
        self.devices(taken=dev(age=4))
        code, _, err = run(["add", "taken", "--mqtt", "--no-wait"])
        self.assertEqual(code, 1)
        self.assertIn("already reporting", err)

    def test_waits_until_the_device_appears(self):
        self.write("COMPOSE_PROFILES=mqtt\nMQTT_PASSWORD=x\n")
        FakeVM.state["hide_until_query"] = 3
        self.devices(newdev=dev(age=2))
        code, out, _ = run(["add", "newdev", "--mqtt", "--host", "h", "--timeout", "5"])
        self.assertEqual(code, 0)
        self.assertIn("is online", out)

    def test_gives_up_with_a_pointer_to_the_bridge_log(self):
        self.write("COMPOSE_PROFILES=mqtt\nMQTT_PASSWORD=x\n")
        code, out, _ = run(["add", "ghost", "--mqtt", "--host", "h", "--timeout", "1"])
        self.assertEqual(code, 1)
        self.assertIn("docker compose logs mqtt-bridge", out)

    def test_the_agent_instructions_are_unchanged_without_the_flag(self):
        _, out, _ = run(["add", "kitchen-pi", "--host", "10.0.0.5", "--no-wait"])
        self.assertIn("--device-id kitchen-pi --endpoint 10.0.0.5:4317", out)
        self.assertNotIn("MQTT", out)


class ViaTests(WithEnv, WithFakeVM):
    def setUp(self):
        WithFakeVM.setUp(self)
        WithEnv.setUp(self)

    def test_check_and_json_say_how_a_device_reports(self):
        self.devices(**{"board": dev(job="mqtt-bridge"), "pi": dev()})
        got = fleet.fetch_devices()
        self.assertEqual((got["board"]["via"], got["pi"]["via"]), ("mqtt", "agent"))
        _, out, _ = run(["check", "board"])
        self.assertIn("via         MQTT", out)
        _, out, _ = run(["check", "pi"])
        self.assertNotIn("via", out)


class FilesTests(unittest.TestCase):
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_env_is_gitignored_because_it_holds_a_password(self):
        with open(os.path.join(self.ROOT, ".gitignore")) as f:
            self.assertIn(".env", f.read().split())

    def test_compose_services_match_what_the_cli_expects(self):
        with open(os.path.join(self.ROOT, "docker-compose.yml")) as f:
            text = f.read()
        self.assertIn(f"image: {fleet.BRIDGE_IMAGE}", text)
        self.assertRegex(text, r'mosquitto:\n    profiles: \["mqtt"\]')
        self.assertRegex(text, r'mqtt-bridge:\n    profiles: \["mqtt", "mqtt-bridge"\]')
        self.assertIsNone(re.search(r"\$\{MQTT_PASSWORD:\?", text), "a required variable would break every compose command")


if __name__ == "__main__":
    unittest.main()
