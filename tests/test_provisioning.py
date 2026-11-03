"""Keeps the three places that decide ONLINE/OFFLINE on the same rule: the dashboard, the alert rule and the CLI.
Stdlib only; reads the provisioning files as text.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGE = "max by (device_id) (time() - tlast_over_time(device_uptime_seconds[1d]))"
LIMIT = ("(clamp_min(3 * max by (device_id) (last_over_time(device_report_interval_seconds[1d])), 30) "
         f"or (0 * {AGE} + 30))")


def read(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


class OfflineRuleTests(unittest.TestCase):
    def test_dashboard_uses_the_per_device_limit_everywhere(self):
        dash = json.loads(read("grafana", "dashboards", "device-fleet.json"))
        exprs = {t["refId"] + "@" + p["title"]: t["expr"] for p in dash["panels"] for t in p.get("targets", [])
                 if "tlast_over_time" in t["expr"] and "time()" in t["expr"]}
        self.assertEqual(len(exprs), 3, exprs)  # the two stat panels and the table's state column
        for where, expr in exprs.items():
            self.assertIn(LIMIT, expr, where)
            self.assertNotRegex(expr, r"(<=|>|> bool) 30\b", f"{where} still has the fixed 30 s rule")

    def test_alert_rule_uses_the_same_limit(self):
        rule = read("grafana", "provisioning", "alerting", "device-offline.yml")
        self.assertIn(f"expr: {AGE}\n", rule)
        self.assertIn(f"expr: {LIMIT}\n", rule)
        self.assertIn("expression: $B > $M", rule)

    def test_alert_summary_names_the_limit_it_used(self):
        rule = read("grafana", "provisioning", "alerting", "device-offline.yml")
        self.assertIn("$values.M.Value", rule)
        self.assertNotIn("threshold 30 s", rule)

    def test_contract_states_the_same_rule(self):
        contract = read("docs", "CONTRACT.md")
        self.assertIn("device_report_interval_seconds", contract)
        self.assertIn(LIMIT, contract)

    def test_floor_and_factor_in_the_cli_match_the_queries(self):
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("fleet_cli", os.path.join(ROOT, "fleet"))
        spec = importlib.util.spec_from_loader("fleet_cli", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        m = re.search(r"clamp_min\((\d+) \* .*?, (\d+)\)", LIMIT)
        self.assertEqual((mod.OFFLINE_FACTOR, mod.OFFLINE_FLOOR), (int(m.group(1)), int(m.group(2))))


if __name__ == "__main__":
    unittest.main()
