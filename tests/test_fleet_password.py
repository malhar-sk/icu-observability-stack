"""Tests for the Grafana admin password: a random one on a first install, `fleet password`, and the services that now come
back by themselves after a power cut. Stdlib only; the fake Grafana from test_fleet_notify stands in for the real one. The
real thing (a fresh install, an existing one, a restarted Docker daemon) was run live: docs/VALIDATION.md, run K.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import base64
import contextlib
import io
import os
import re
import stat
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_fleet import fleet, run  # noqa: E402
from test_fleet_mqtt import WithEnv  # noqa: E402
from test_fleet_notify import WithFakeGrafana  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def basic(user_pass):
    return "Basic " + base64.b64encode(user_pass.encode()).decode()


class AuthTests(WithEnv):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(fleet, "GRAFANA_AUTH", None)
        p.start()
        self.addCleanup(p.stop)

    def test_the_default_when_nothing_is_set(self):
        self.assertEqual(fleet.grafana_auth(), "admin:admin")

    def test_the_password_kept_in_env(self):
        self.write("GRAFANA_ADMIN_PASSWORD=abc123\n")
        self.assertEqual(fleet.grafana_auth(), "admin:abc123")

    def test_an_explicit_setting_wins(self):
        self.write("GRAFANA_ADMIN_PASSWORD=abc123\n")
        with mock.patch.object(fleet, "GRAFANA_AUTH", "admin:override"):
            self.assertEqual(fleet.grafana_auth(), "admin:override")

    def test_a_quoted_password_is_read_back_unquoted(self):
        fleet.write_env({"GRAFANA_ADMIN_PASSWORD": "has space$"})
        self.assertEqual(fleet.grafana_auth(), "admin:has space$")


class PrepareTests(WithEnv):
    def volumes(self, names, code=0):
        def fake(cmd, **kw):
            self.calls.append(cmd)
            return mock.Mock(returncode=code, stdout="\n".join(names), stderr="")
        p = mock.patch.object(fleet.subprocess, "run", fake)
        p.start()
        self.addCleanup(p.stop)

    def test_a_first_install_gets_a_random_password_kept_private(self):
        self.volumes(["other_vm_data"])
        pw = fleet.prepare_grafana_password()
        self.assertRegex(pw, r"^[0-9a-f]{24}$")
        self.assertEqual(fleet.read_env()["GRAFANA_ADMIN_PASSWORD"], pw)
        self.assertEqual(stat.S_IMODE(os.stat(self.env_path).st_mode), 0o600)

    def test_each_install_gets_a_different_one(self):
        self.volumes([])
        first = fleet.prepare_grafana_password()
        os.remove(self.env_path)
        self.assertNotEqual(first, fleet.prepare_grafana_password())

    def test_an_existing_grafana_is_left_alone(self):
        for name in ("icu-observability-stack_grafana_data", "grafana_data", "x_grafana_data"):
            self.volumes([name, "x_vm_data"])
            self.assertIsNone(fleet.prepare_grafana_password(), name)
            self.assertFalse(os.path.exists(self.env_path), name)

    def test_a_volume_that_only_looks_similar_does_not_count(self):
        self.volumes(["x_grafana_data_backup", "mygrafana_dat"])
        self.assertIsNotNone(fleet.prepare_grafana_password())

    def test_a_password_already_in_env_is_kept_and_docker_is_not_asked(self):
        self.write("GRAFANA_ADMIN_PASSWORD=mine\n")
        self.volumes([])
        self.assertIsNone(fleet.prepare_grafana_password())
        self.assertEqual(self.calls, [])
        self.assertEqual(fleet.read_env()["GRAFANA_ADMIN_PASSWORD"], "mine")

    def test_when_docker_cannot_say_nothing_is_changed(self):
        self.volumes([], code=1)  # if Grafana did exist, a new password in .env would no longer match it
        self.assertIsNone(fleet.prepare_grafana_password())
        self.assertFalse(os.path.exists(self.env_path))


class UpOutputTests(WithEnv):
    def setUp(self):
        super().setUp()
        self.vols = []

        def fake(cmd, **kw):
            self.calls.append(cmd)
            return mock.Mock(returncode=0, stdout="\n".join(self.vols) if cmd[:3] == ["docker", "volume", "ls"] else "", stderr="")
        for name, value in [("require_docker", lambda: None), ("missing_images", lambda: []), ("compose_ps", lambda: {}),
                            ("service_checks", lambda: {"victoriametrics": (True, "healthy")}), ("lan_ip", lambda: "10.0.0.5"),
                            ("fetch_devices", lambda: {}), ("notify_configured", lambda: None), ("port_open", lambda *a, **k: False),
                            ("dashboard_state", lambda: "ok")]:
            p = mock.patch.object(fleet, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(fleet.subprocess, "run", fake)
        p.start()
        self.addCleanup(p.stop)

    def test_a_first_install_shows_the_new_password_once(self):
        _, out, _ = run(["up"])
        pw = fleet.read_env()["GRAFANA_ADMIN_PASSWORD"]
        self.assertIn(f"admin / {pw}", out)
        self.assertIn("kept in .env", out)
        self.assertNotIn("admin / admin", out)
        self.vols = ["x_grafana_data"]  # it exists now
        _, out, _ = run(["up"])
        self.assertNotIn(pw, out)  # not printed on every start
        self.assertIn("the password in .env", out)

    def test_an_existing_install_with_the_default_is_told_so(self):
        self.vols = ["x_grafana_data"]
        _, out, _ = run(["up"])
        self.assertIn("admin / admin", out)
        self.assertIn("./fleet password", out)
        self.assertFalse(os.path.exists(self.env_path))  # nothing was changed behind their back

    def test_a_login_that_is_refused_is_explained(self):
        self.vols = ["x_grafana_data"]
        with mock.patch.object(fleet, "dashboard_state", lambda: "login"):
            _, out, _ = run(["up"])
        self.assertIn("the password you set", out)
        self.assertNotIn("admin / admin", out)

    def test_the_mqtt_and_other_settings_survive(self):
        self.write("COMPOSE_PROFILES=mqtt\nMQTT_PASSWORD=x\n")
        run(["up"])
        env = fleet.read_env()
        self.assertEqual((env["COMPOSE_PROFILES"], env["MQTT_PASSWORD"]), ("mqtt", "x"))
        self.assertIn("GRAFANA_ADMIN_PASSWORD", env)


class PasswordCommandTests(WithEnv, WithFakeGrafana):
    def setUp(self):
        WithFakeGrafana.setUp(self)
        WithEnv.setUp(self)
        p = mock.patch.object(fleet, "GRAFANA_AUTH", None)  # use the password in .env, as a real run does
        p.start()
        self.addCleanup(p.stop)

    def put_calls(self):
        return [b for m, p, b in self.st["calls"] if m == "PUT" and p == "/api/user/password"]

    def test_a_random_password_replaces_the_default_in_grafana_and_in_env(self):
        code, out, _ = run(["password"])
        self.assertEqual(code, 0)
        new = fleet.read_env()["GRAFANA_ADMIN_PASSWORD"]
        self.assertRegex(new, r"^[0-9a-f]{24}$")
        self.assertEqual(self.put_calls(), [{"oldPassword": "admin", "newPassword": new, "confirmNew": new}])
        self.assertEqual(self.st["password"], new)
        self.assertIn(f"admin / {new}", out)

    def test_the_second_change_uses_the_first_as_the_old_password(self):
        run(["password"])
        first = fleet.read_env()["GRAFANA_ADMIN_PASSWORD"]
        code, _, _ = run(["password"])
        self.assertEqual(code, 0)
        self.assertEqual(self.put_calls()[1]["oldPassword"], first)
        self.assertNotEqual(fleet.read_env()["GRAFANA_ADMIN_PASSWORD"], first)

    def test_the_other_commands_use_the_new_password_afterwards(self):
        run(["password"])
        new = fleet.read_env()["GRAFANA_ADMIN_PASSWORD"]
        self.st["auth_seen"].clear()
        code, _, _ = run(["notify", "status"])
        self.assertEqual(code, 0)
        self.assertTrue(self.st["auth_seen"])
        self.assertEqual(set(self.st["auth_seen"]), {basic(f"admin:{new}")})

    def test_a_refusal_by_grafana_changes_nothing(self):
        self.st["password"] = "something-else"  # .env does not know it, so the old password is wrong
        code, _, err = run(["password"])
        self.assertEqual(code, 1)
        self.assertIn("Invalid old password", err)
        self.assertIn("Nothing was changed", err)
        self.assertFalse(os.path.exists(self.env_path))
        self.assertIn("something-else", self.st["password"])

    def test_you_can_type_your_own_and_it_is_not_echoed(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch.object(fleet.getpass, "getpass", side_effect=["my-own-passphrase", "my-own-passphrase"]):
            code, out, _ = run(["password", "--ask"])
        self.assertEqual(code, 0)
        self.assertEqual(fleet.read_env()["GRAFANA_ADMIN_PASSWORD"], "my-own-passphrase")
        self.assertEqual(self.st["password"], "my-own-passphrase")
        self.assertNotIn("my-own-passphrase", out)

    def test_ask_refuses_a_mismatch_a_short_one_and_a_missing_terminal(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=True):
            with mock.patch.object(fleet.getpass, "getpass", side_effect=["long-enough-1", "long-enough-2"]):
                self.assertIn("differ", run(["password", "--ask"])[2])
            with mock.patch.object(fleet.getpass, "getpass", side_effect=["short", "short"]):
                self.assertIn("at least 8", run(["password", "--ask"])[2])
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            self.assertIn("needs a terminal", run(["password", "--ask"])[2])
        self.assertEqual(self.put_calls(), [])

    def test_a_password_that_env_cannot_hold_is_refused_before_grafana_is_touched(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch.object(fleet.getpass, "getpass", side_effect=["it's-a-long-one", "it's-a-long-one"]):
            code, _, err = run(["password", "--ask"])
        self.assertEqual(code, 1)
        self.assertIn("quote", err)
        self.assertEqual(self.put_calls(), [])  # so Grafana and .env cannot end up disagreeing
        self.assertEqual(self.st["password"], "admin")

    def test_show_prints_the_password_without_touching_anything(self):
        self.write("GRAFANA_ADMIN_PASSWORD=abc123\n")
        code, out, _ = run(["password", "--show"])
        self.assertEqual((code, out.strip()), (0, "abc123"))
        self.assertEqual(self.put_calls(), [])

    def test_show_for_an_install_that_still_has_the_default_says_so(self):
        _, out, _ = run(["password", "--show"])
        self.assertIn("admin", out)
        self.assertIn("./fleet password", out)

    def test_show_works_without_a_running_stack_and_change_does_not(self):
        from test_fleet import FakeVM
        FakeVM.state["healthy"] = False
        self.write("GRAFANA_ADMIN_PASSWORD=abc123\n")
        self.assertEqual(run(["password", "--show"])[0], 0)
        code, _, err = run(["password"])
        self.assertEqual(code, 1)
        self.assertIn("not running", err)

    def test_the_two_options_exclude_each_other(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            fleet.build_parser().parse_args(["password", "--ask", "--show"])

    def test_the_login_error_tells_you_how_to_fix_it(self):
        self.st["auth_ok"] = False
        _, _, err = run(["notify", "status"])
        self.assertIn("GRAFANA_ADMIN_PASSWORD", err)
        self.assertIn("FLEET_GRAFANA_AUTH", err)


class FilesTests(unittest.TestCase):
    def compose(self):
        with open(os.path.join(ROOT, "docker-compose.yml")) as f:
            return f.read()

    def service(self, name):
        text = self.compose()
        block = text[text.index(f"  {name}:\n"):]
        return block[:block.index("\n\n")]

    def test_the_core_services_come_back_after_a_power_cut(self):
        for name in ("victoriametrics", "grafana", "otel-collector"):
            self.assertIn("restart: unless-stopped", self.service(name), name)

    def test_every_service_that_runs_by_default_or_on_request_restarts(self):
        for name in ("mosquitto", "mqtt-bridge", "watcher", "checkin"):
            self.assertIn("restart: unless-stopped", self.service(name), name)

    def test_grafanas_first_run_password_comes_from_env_with_a_safe_fallback(self):
        self.assertIn("GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD:-admin}", self.service("grafana"))
        self.assertIsNone(re.search(r"\$\{[A-Z_]+:\?", self.compose()), "a required variable would break every compose command")

    def test_the_scripts_read_the_password_instead_of_assuming_admin(self):
        for name in ("validate.sh", "validate_mqtt.sh", "validate_watch.sh", "validate_checkin.sh"):
            with open(os.path.join(ROOT, "scripts", name)) as f:
                text = f.read()
            self.assertIn("grafana_pw()", text, name)
            self.assertNotIn(':-admin:admin}"', text, name)
        for name in ("validate_mqtt.sh", "validate_watch.sh", "validate_checkin.sh"):
            with open(os.path.join(ROOT, "scripts", name)) as f:
                self.assertRegex(f.read(), r"fleet up[^\n]*; then refresh_auth", name)  # a first `up` creates the password


if __name__ == "__main__":
    unittest.main()
