"""Tests for `fleet notify`. Stdlib only. A fake Grafana HTTP server stands in for the real one; its behaviour
(routes, the receiver-test API, the applied-config history) was copied from what a real Grafana 13.3 did
when these commands were run against it. No Docker needed.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import base64
import json
import os
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_fleet import WithFakeVM, fleet, run  # noqa: E402

DEFAULT_POLICY = {"receiver": "empty", "group_by": ["grafana_folder", "alertname"]}
RECEIVER = fleet.receiver_id(fleet.NOTIFY_UID)


class FakeGrafana(BaseHTTPRequestHandler):
    state = {}

    def log_message(self, *a):
        pass

    def _send(self, code, obj=None):
        body = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        return json.loads(raw) if raw else None

    def _route(self, method):
        st = FakeGrafana.state
        path = urllib.parse.urlparse(self.path).path
        if not st["auth_ok"]:
            return self._send(401, {"message": "Unauthorized"})
        body = self._body() if method in ("POST", "PUT") else None
        st["calls"].append((method, path, body))
        if method in ("POST", "PUT", "DELETE") and not path.endswith("/test"):
            st["dirty"], st["polls_since_write"] = True, 0
        cp = "/api/v1/provisioning/contact-points"
        if path == "/api/v1/provisioning/policies":
            if method == "GET":
                return self._send(200, st["policy"])
            if method == "PUT":
                st["policy"] = body
                return self._send(202, {"message": "policies updated"})
            if method == "DELETE":
                st["policy"] = dict(DEFAULT_POLICY)
                return self._send(202, {"message": "policies deleted"})
        if path == cp and method == "POST":
            st["contact"] = body
            return self._send(202, body)
        if path == cp and method == "GET":
            return self._send(200, [st["contact"]] if st["contact"] else [])
        if path == f"{cp}/{fleet.NOTIFY_UID}" and method == "PUT":
            st["contact"] = body
            return self._send(202, body)
        if path == f"{cp}/{fleet.NOTIFY_UID}" and method == "DELETE":
            st["contact"] = None
            return self._send(202, {"message": "deleted"})
        if path == f"{fleet.NOTIFY_API}/receivers/{RECEIVER}" and method == "GET":
            c = st["contact"]
            if not c:
                return self._send(404, {"message": "not found"})
            secret_url = c["type"] != "webhook"  # Grafana hides these when reading back
            integ = {"uid": c["uid"], "type": c["type"], "version": "v1",
                     "settings": {} if secret_url or c["type"] == "telegram" else
                     {"url": c["settings"]["url"], **({"payload": c["settings"]["payload"]} if "payload" in c["settings"] else {})},
                     "secureFields": {k: True for k in c["settings"]} if secret_url else {}}
            return self._send(200, {"spec": {"title": c["name"], "integrations": [integ]}})
        if path == f"{fleet.NOTIFY_API}/receivers/{RECEIVER}/test" and method == "POST":
            return self._send(200, {"status": st["test_status"], "duration": "2ms",
                                    **({"error": st["test_error"]} if st["test_status"] != "success" else {})})
        if path == "/api/alertmanager/grafana/config/api/v1/receivers":
            c = st["contact"]
            running = st["running_type"] or (c and c["type"])
            return self._send(200, [{"name": "empty", "integrations": []}] + (
                [{"name": fleet.NOTIFY_UID, "integrations": [{"name": running, "sendResolved": True, **st["attempt"]}]}]
                if c else []))
        if path == "/api/alertmanager/grafana/config/history":
            # Like the real one: a saved change only shows up as a NEW applied entry some polls later.
            st["history_polls"] += 1
            if st["dirty"]:
                st["polls_since_write"] += 1
                if st["polls_since_write"] > st["bump_after"]:
                    st["history_id"] += 1
                    st["dirty"] = False
            return self._send(200, [{"id": st["history_id"]}])
        return self._send(404, {"message": "Not found"})

    do_GET = lambda self: self._route("GET")
    do_POST = lambda self: self._route("POST")
    do_PUT = lambda self: self._route("PUT")
    do_DELETE = lambda self: self._route("DELETE")


class WithFakeGrafana(WithFakeVM):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.gserver = HTTPServer(("127.0.0.1", 0), FakeGrafana)
        threading.Thread(target=cls.gserver.serve_forever, daemon=True).start()
        cls.gurl = f"http://127.0.0.1:{cls.gserver.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.gserver.shutdown()
        cls.gserver.server_close()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        FakeGrafana.state = {"auth_ok": True, "policy": dict(DEFAULT_POLICY), "contact": None, "calls": [],
                             "test_status": "success", "test_error": "", "running_type": None,
                             "attempt": {"lastNotifyAttempt": "0001-01-01T00:00:00.000Z"},
                             "history_polls": 0, "bump_after": 0, "history_id": 10, "dirty": False,
                             "polls_since_write": 0}
        for target, value in (("GRAFANA_URL", self.gurl), ("GRAFANA_AUTH", "admin:admin")):
            p = mock.patch.object(fleet, target, value)
            p.start()
            self.addCleanup(p.stop)

    @property
    def st(self):
        return FakeGrafana.state

    def writes(self):
        return [(m, p) for m, p, _ in self.st["calls"] if m in ("POST", "PUT", "DELETE")
                and not p.endswith("/test")]


class SetTests(WithFakeGrafana):
    URL = "https://example.test/hooks/SECRET-PART"

    def test_webhook_is_saved_routed_tested_and_activated(self):
        code, out, err = run(["notify", "set", "webhook", self.URL])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.st["contact"], {"uid": "fleet-notify", "name": "fleet-notify", "type": "webhook",
                                              "settings": {"url": self.URL}, "disableResolveMessage": False})
        self.assertEqual(self.st["policy"], {"receiver": "fleet-notify", "group_by": ["alertname", "device_id"],
                                             "group_wait": "10s", "group_interval": "1m", "repeat_interval": "4h"})
        test_calls = [b for m, p, b in self.st["calls"] if p.endswith("/test")]
        self.assertEqual(test_calls[0]["integration"]["type"], "webhook")
        self.assertIn("Active", out)

    def test_the_secret_never_appears_in_output(self):
        _, out, err = run(["notify", "set", "webhook", self.URL, "--no-wait"])
        _, status_out, _ = run(["notify", "status"])
        for text in (out, err, status_out):
            self.assertNotIn("SECRET-PART", text)

    def test_second_set_updates_instead_of_creating(self):
        run(["notify", "set", "slack", self.URL, "--no-wait"])
        run(["notify", "set", "discord", self.URL, "--no-wait"])
        methods = [(m, p) for m, p in self.writes() if "contact-points" in p]
        self.assertEqual(methods, [("POST", "/api/v1/provisioning/contact-points"),
                                   ("PUT", "/api/v1/provisioning/contact-points/fleet-notify")])
        self.assertEqual(self.st["contact"]["type"], "discord")

    def test_telegram_maps_token_and_chat(self):
        code, _, _ = run(["notify", "set", "telegram", "123:ABC", "-100777", "--no-wait"])
        self.assertEqual(code, 0)
        self.assertEqual(self.st["contact"]["settings"], {"bottoken": "123:ABC", "chatid": "-100777"})

    def test_wrong_number_of_values(self):
        code, _, err = run(["notify", "set", "telegram", "only-one", "--no-wait"])
        self.assertEqual(code, 1)
        self.assertIn("bot-token chat-id", err)
        self.assertEqual(self.writes(), [])

    def test_rejects_things_that_are_not_urls(self):
        for bad in ["not a url", "ftp://x.test/y", "http://", "   "]:
            code, _, err = run(["notify", "set", "webhook", bad, "--no-wait"])
            self.assertEqual(code, 1, bad)
        self.assertEqual(self.writes(), [])

    def test_asks_with_hidden_input_when_values_are_missing(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch.object(fleet.getpass, "getpass", return_value=self.URL) as asked:
            code, _, _ = run(["notify", "set", "discord", "--no-wait"])
        self.assertEqual(code, 0)
        self.assertEqual(asked.call_count, 1)
        self.assertEqual(self.st["contact"]["settings"]["url"], self.URL)

    def test_no_terminal_and_no_values_is_an_error_not_a_hang(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            code, _, err = run(["notify", "set", "slack"])
        self.assertEqual(code, 1)
        self.assertIn("no terminal", err)

    def test_failed_test_is_reported_and_exits_nonzero(self):
        self.st["test_status"], self.st["test_error"] = "failure", "no such host"
        code, out, _ = run(["notify", "set", "telegram", "1:a", "2", "--no-wait"])
        self.assertEqual(code, 1)
        self.assertIn("no such host", out)
        self.assertEqual(self.st["contact"]["type"], "telegram")  # still saved, so the user can fix and retry

    def test_waits_for_grafana_to_switch_over(self):
        self.st["bump_after"] = 4  # the applied-config history changes after a few polls
        with mock.patch.object(fleet.time, "sleep", lambda s: None):
            code, out, _ = run(["notify", "set", "webhook", self.URL])
        self.assertEqual(code, 0)
        self.assertIn("Active", out)
        self.assertGreater(self.st["history_polls"], 4)

    def test_says_so_when_grafana_does_not_switch_over_in_time(self):
        self.st["bump_after"] = 10 ** 6
        with mock.patch.object(fleet, "wait_until_applied", return_value=False):
            code, out, _ = run(["notify", "set", "webhook", self.URL])
        self.assertEqual(code, 0)
        self.assertNotIn("Active", out)
        self.assertIn("has not switched over yet", out)

    def test_no_wait_warns_that_the_old_setting_is_still_live(self):
        code, out, _ = run(["notify", "set", "webhook", self.URL, "--no-wait"])
        self.assertEqual(code, 0)
        self.assertIn("until then alerts use the old setting", out)
        self.assertEqual(self.st["history_polls"], 1)  # only the "before" read


class PolicyGuardTests(WithFakeGrafana):
    def test_untouched_grafana_policies_are_safe_to_replace(self):
        for receiver in ("empty", "grafana-default-email", "fleet-notify"):
            self.st["policy"] = {"receiver": receiver}
            code, _, _ = run(["notify", "set", "webhook", "https://x.test/h", "--no-wait"])
            self.assertEqual(code, 0, receiver)

    def test_a_customised_policy_is_not_overwritten_without_force(self):
        custom = {"receiver": "empty", "routes": [{"receiver": "empty", "object_matchers": [["a", "=", "b"]]}]}
        for policy in (custom, {"receiver": "my-own-email"}):
            self.st["policy"] = policy
            before = list(self.st["calls"])
            code, _, err = run(["notify", "set", "webhook", "https://x.test/h", "--no-wait"])
            self.assertEqual(code, 1)
            self.assertIn("customised", err)
            self.assertEqual(self.st["policy"], policy)
            self.assertEqual([c for c in self.st["calls"][len(before):] if c[0] != "GET"], [])

    def test_force_replaces_it(self):
        self.st["policy"] = {"receiver": "my-own-email", "routes": [{"receiver": "x"}]}
        code, _, _ = run(["notify", "set", "webhook", "https://x.test/h", "--no-wait", "--force"])
        self.assertEqual(code, 0)
        self.assertEqual(self.st["policy"]["receiver"], "fleet-notify")
        self.assertNotIn("routes", self.st["policy"])


class TestAndStatusTests(WithFakeGrafana):
    def configure(self, kind="webhook", url="https://hooks.test/path/SECRET"):
        self.st["contact"] = {"uid": "fleet-notify", "name": "fleet-notify", "type": kind,
                              "settings": {"url": url}, "disableResolveMessage": False}
        self.st["policy"] = {"receiver": "fleet-notify"}

    def test_test_with_nothing_set_up(self):
        code, _, err = run(["notify", "test"])
        self.assertEqual(code, 1)
        self.assertIn("no notifications are set up", err)

    def test_test_success_does_not_overclaim(self):
        self.configure()
        code, out, _ = run(["notify", "test"])
        self.assertEqual(code, 0)
        self.assertIn("not that you have seen it", out)

    def test_test_uses_the_saved_integration_not_a_new_secret(self):
        self.configure("slack")
        run(["notify", "test"])
        sent = [b for m, p, b in self.st["calls"] if p.endswith("/test")][0]["integration"]
        self.assertEqual((sent["uid"], sent["type"], sent["secureFields"]), ("fleet-notify", "slack", {"url": True}))
        self.assertNotIn("SECRET", json.dumps(sent))

    def test_test_failure(self):
        self.configure()
        self.st["test_status"], self.st["test_error"] = "failure", "HTTP 404 from the server"
        code, out, _ = run(["notify", "test"])
        self.assertEqual(code, 1)
        self.assertIn("HTTP 404 from the server", out)

    def test_status_off(self):
        code, out, _ = run(["notify", "status"])
        self.assertEqual(code, 0)
        self.assertIn("off", out)

    def test_status_shows_only_scheme_and_host(self):
        self.configure()
        _, out, _ = run(["notify", "status"])
        self.assertIn("https://hooks.test/...", out)
        self.assertNotIn("SECRET", out)
        self.assertNotIn("/path", out)

    def test_status_for_a_type_whose_address_is_secret(self):
        self.configure("discord")
        _, out, _ = run(["notify", "status"])
        self.assertIn("stored encrypted by Grafana", out)

    def test_status_notices_a_policy_that_does_not_route_to_it(self):
        self.configure()
        self.st["policy"] = {"receiver": "somewhere-else"}
        _, out, _ = run(["notify", "status"])
        self.assertIn("NOT routed", out)

    def test_status_flags_a_change_that_is_not_live_yet(self):
        self.configure("slack")
        self.st["running_type"] = "webhook"
        _, out, _ = run(["notify", "status"])
        self.assertIn("Change not live yet", out)

    def test_status_last_delivery_variants(self):
        self.configure()
        _, out, _ = run(["notify", "status"])
        self.assertIn("none yet", out)
        self.st["attempt"] = {"lastNotifyAttempt": "2026-11-02T01:39:00.012Z"}
        _, out, _ = run(["notify", "status"])
        self.assertIn("ok at 2026-11-02T01:39:00", out)
        self.st["attempt"] = {"lastNotifyAttempt": "2026-11-02T01:40:00.000Z", "lastNotifyAttemptError": "HTTP 500"}
        _, out, _ = run(["notify", "status"])
        self.assertIn("FAILED", out)
        self.assertIn("HTTP 500", out)

    def test_a_wrong_grafana_password_gets_advice(self):
        self.st["auth_ok"] = False
        code, _, err = run(["notify", "status"])
        self.assertEqual(code, 1)
        self.assertIn("FLEET_GRAFANA_AUTH", err)

    def test_other_commands_do_not_depend_on_the_grafana_login(self):
        self.st["auth_ok"] = False
        self.assertIsNone(fleet.notify_configured())  # "unknown", used only for hints


class OffTests(WithFakeGrafana):
    def setUp(self):
        super().setUp()
        run(["notify", "set", "webhook", "https://x.test/h", "--no-wait"])

    def test_off_removes_it_and_restores_default_routing(self):
        code, out, _ = run(["notify", "off", "--yes"])
        self.assertEqual(code, 0)
        self.assertIsNone(self.st["contact"])
        self.assertEqual(self.st["policy"], DEFAULT_POLICY)
        self.assertIn("within about a minute", out)

    def test_declining_changes_nothing(self):
        with mock.patch.object(fleet, "confirm", return_value=False):
            code, out, _ = run(["notify", "off"])
        self.assertEqual(code, 1)
        self.assertIn("Aborted", out)
        self.assertIsNotNone(self.st["contact"])

    def test_off_leaves_a_policy_it_does_not_own_alone(self):
        self.st["policy"] = {"receiver": "my-own", "routes": [{"receiver": "fleet-notify"}]}
        run(["notify", "off", "--yes"])
        self.assertEqual(self.st["policy"]["receiver"], "my-own")

    def test_off_when_already_off(self):
        run(["notify", "off", "--yes"])
        code, out, _ = run(["notify", "off", "--yes"])
        self.assertEqual(code, 0)
        self.assertIn("already off", out)


class MiscTests(WithFakeGrafana):
    def test_the_hints_in_up_and_status_depend_on_whether_it_is_set_up(self):
        self.assertIs(fleet.notify_configured(), False)
        run(["notify", "set", "webhook", "https://x.test/h", "--no-wait"])
        self.assertIs(fleet.notify_configured(), True)

    def test_unreachable_grafana_means_unknown_not_a_crash(self):
        with mock.patch.object(fleet, "GRAFANA_URL", "http://127.0.0.1:9"):
            self.assertIsNone(fleet.notify_configured())

    def test_a_closed_pipe_is_not_an_error(self):
        # Found by running `./fleet notify status | head -1`: it used to print a traceback.
        with mock.patch.object(fleet, "cmd_status", side_effect=BrokenPipeError):
            with mock.patch.object(fleet.os, "dup2"):
                self.assertEqual(fleet.main(["status"]), 0)


if __name__ == "__main__":
    unittest.main()
