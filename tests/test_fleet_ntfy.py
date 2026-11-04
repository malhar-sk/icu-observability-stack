"""Tests for `fleet notify set ntfy`. Stdlib only. The fake Grafana from test_fleet_notify stands in for the real one.
The payload template was run through a real Grafana 13.3 against a stand-in ntfy server (docs/VALIDATION.md, run J): it
renders to valid ntfy JSON. A Go template cannot be run here, so a "golden" test pins the exact text that was validated:
if you change the template, run it through Grafana again (scripts or by hand) before updating the test.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import json
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_fleet import fleet, run  # noqa: E402
from test_fleet_notify import WithFakeGrafana  # noqa: E402

GOLDEN = ('{"topic":"@TOPIC@","title":"{{ if eq .Status "firing" }}Device offline{{ else }}Device back online{{ end }}",'
          '"message":"{{ range .Alerts }}{{ if eq .Status "firing" }}{{ .Annotations.summary }}'
          '{{ else }}{{ .Labels.device_id }} is reporting again{{ end }}\\n{{ end }}",'
          '"priority":{{ if eq .Status "firing" }}4{{ else }}3{{ end }},'
          '"tags":["{{ if eq .Status "firing" }}rotating_light{{ else }}white_check_mark{{ end }}"]}')


class SettingsTests(unittest.TestCase):
    def test_a_bare_topic_goes_to_ntfy_sh(self):
        s = fleet.ntfy_settings(["mytopic"])
        self.assertEqual((s["url"], s["httpMethod"]), ("https://ntfy.sh/", "POST"))
        self.assertIn('"topic":"mytopic"', s["payload"]["template"])

    def test_a_full_address_means_your_own_server(self):
        for text, url, topic in [("https://ntfy.example.org/alerts", "https://ntfy.example.org/", "alerts"),
                                 ("http://10.0.0.5:8080/alerts/", "http://10.0.0.5:8080/", "alerts"),
                                 ("https://example.org/ntfy/alerts", "https://example.org/ntfy/", "alerts")]:
            s = fleet.ntfy_settings([text])
            self.assertEqual(s["url"], url, text)
            self.assertIn(f'"topic":"{topic}"', s["payload"]["template"], text)

    def test_bad_topics_and_addresses_are_refused(self):
        for bad in ["", "a b", "a.b", "x/y", "a" * 65, 'q"uote', "back\\slash", "new\nline", "topic}{", "$(id)", "é",
                    "ftp://x.test/t", "https://", "https://x.test/", "https://x.test/t?x=1", "https://x.test/t#f",
                    "https://x.test/a b"]:
            with self.assertRaises(fleet.FleetError, msg=repr(bad)):
                fleet.ntfy_settings([bad])

    def test_only_safe_characters_can_reach_the_template(self):
        # the topic is pasted into JSON text: this is what keeps it from breaking out of it
        for topic in ["a", "A-z_0-9", "x" * 64]:
            template = fleet.ntfy_settings([topic])["payload"]["template"]
            self.assertIn(f'"topic":"{topic}"', template)
            self.assertEqual(template.count('"topic"'), 1)  # one place, and nothing else was added around it

    def test_the_template_is_the_one_that_was_validated_against_grafana(self):
        self.assertEqual(fleet.NTFY_TEMPLATE, GOLDEN)

    def test_the_template_makes_one_message_with_the_fields_ntfy_reads(self):
        for field in ('"topic"', '"title"', '"message"', '"priority"', '"tags"'):
            self.assertIn(field, fleet.NTFY_TEMPLATE)
        self.assertIn("rotating_light", fleet.NTFY_TEMPLATE)      # offline
        self.assertIn("white_check_mark", fleet.NTFY_TEMPLATE)    # back
        self.assertEqual(fleet.NTFY_TEMPLATE.count("{{"), fleet.NTFY_TEMPLATE.count("}}"))
        self.assertEqual(len(re.findall(r"\{\{ if ", fleet.NTFY_TEMPLATE)) + 1, len(re.findall(r"\{\{ end \}\}", fleet.NTFY_TEMPLATE)))  # +1: range


class SetTests(WithFakeGrafana):
    def test_it_is_saved_as_a_webhook_that_publishes_to_ntfy(self):
        code, out, err = run(["notify", "set", "ntfy", "longsecrettopic123", "--no-wait"])
        self.assertEqual(code, 0, out + err)
        c = self.st["contact"]
        self.assertEqual((c["type"], c["settings"]["url"], c["settings"]["httpMethod"]), ("webhook", "https://ntfy.sh/", "POST"))
        self.assertIn('"topic":"longsecrettopic123"', c["settings"]["payload"]["template"])
        test_calls = [b for m, p, b in self.st["calls"] if p.endswith("/test")]
        self.assertEqual(test_calls[0]["integration"]["type"], "webhook")  # the test uses what was saved
        self.assertIn("alerts will go to ntfy", out)

    def test_the_topic_is_not_shown_by_status_or_the_output(self):
        _, out, err = run(["notify", "set", "ntfy", "longsecrettopic123", "--no-wait"])
        _, status, _ = run(["notify", "status"])
        for text in (out, err, status):
            self.assertNotIn("longsecrettopic123", text)
        self.assertIn("Notifications: ntfy", status)
        self.assertIn("https://ntfy.sh/...", status)

    def test_a_public_topic_comes_with_a_warning_a_private_server_does_not(self):
        _, out, _ = run(["notify", "set", "ntfy", "mytopic", "--no-wait"])
        self.assertIn("anyone who knows the topic name can read your alerts", out)
        _, out, _ = run(["notify", "set", "ntfy", "https://ntfy.example.org/mytopic", "--no-wait"])
        self.assertNotIn("anyone who knows", out)

    def test_a_random_topic_is_made_and_said_once(self):
        code, out, _ = run(["notify", "set", "ntfy", "--random-topic", "--no-wait"])
        self.assertEqual(code, 0)
        topic = re.search(r"Your topic is (fleet-[0-9a-f]{16})", out)
        self.assertIsNotNone(topic, out)
        self.assertIn(f'"topic":"{topic.group(1)}"', self.st["contact"]["settings"]["payload"]["template"])
        self.assertIn("subscribe", out)
        _, status, _ = run(["notify", "status"])
        self.assertNotIn(topic.group(1), status)  # shown once, at the moment it is made
        _, out2, _ = run(["notify", "set", "ntfy", "--random-topic", "--no-wait"])
        self.assertNotIn(topic.group(1), out2)  # a different one each time

    def test_a_random_topic_for_your_own_server_names_no_default_server(self):
        _, out, _ = run(["notify", "set", "ntfy", "--random-topic", "--no-wait"])
        self.assertIn("ntfy.sh", out)

    def test_random_topic_conflicts_are_refused(self):
        code, _, err = run(["notify", "set", "ntfy", "mytopic", "--random-topic", "--no-wait"])
        self.assertEqual(code, 1)
        self.assertIn("leave the topic out", err)
        code, _, err = run(["notify", "set", "discord", "https://x.test/h", "--random-topic", "--no-wait"])
        self.assertEqual(code, 1)
        self.assertIn("only applies to ntfy", err)
        self.assertEqual(self.writes(), [])

    def test_a_bad_topic_changes_nothing(self):
        code, _, err = run(["notify", "set", "ntfy", "not a topic", "--no-wait"])
        self.assertEqual(code, 1)
        self.assertIn("1 to 64 letters", err)
        self.assertEqual(self.writes(), [])

    def test_it_asks_for_the_topic_with_hidden_input(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch.object(fleet.getpass, "getpass", return_value="hiddentopic") as asked:
            code, _, _ = run(["notify", "set", "ntfy", "--no-wait"])
        self.assertEqual(code, 0)
        self.assertEqual(asked.call_count, 1)
        self.assertIn("topic", asked.call_args[0][0])

    def test_switching_between_ntfy_and_another_type_works_both_ways(self):
        run(["notify", "set", "ntfy", "mytopic", "--no-wait"])
        run(["notify", "set", "discord", "https://x.test/h", "--no-wait"])
        self.assertEqual(self.st["contact"]["type"], "discord")
        run(["notify", "set", "ntfy", "mytopic", "--no-wait"])
        self.assertEqual(self.st["contact"]["type"], "webhook")
        self.assertIn("payload", self.st["contact"]["settings"])

    def test_a_plain_webhook_is_still_a_plain_webhook(self):
        run(["notify", "set", "webhook", "https://example.test/hook", "--no-wait"])
        self.assertEqual(self.st["contact"]["settings"], {"url": "https://example.test/hook"})
        _, status, _ = run(["notify", "status"])
        self.assertIn("Notifications: webhook", status)

    def test_the_hints_name_ntfy(self):
        _, out, _ = run(["notify", "status"])
        self.assertIn("<ntfy|telegram|discord|slack|webhook>", out)


class FilesTests(unittest.TestCase):
    def test_ntfy_is_a_choice_and_the_help_explains_it(self):
        self.assertIn("ntfy", fleet.NOTIFY_TYPES)
        _, out, _ = run(["notify", "set", "--help"])
        self.assertIn("ntfy", out)
        self.assertIn("--random-topic", out)
        self.assertIn("https://your.server/topic", out)


if __name__ == "__main__":
    unittest.main()
