"""Tests for agent/install-service.sh. Stdlib only.

These only use the script's --print mode and its input validation, so they change nothing on the
machine running them (no files under /etc or /opt, no systemctl). The real install, restart and
uninstall were run under a real systemd; see docs/VALIDATION.md.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "agent", "install-service.sh")


def run(*args):
    r = subprocess.run(["bash", SCRIPT, *args], capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout, r.stderr


@unittest.skipUnless(shutil.which("bash"), "bash is needed")
class PrintModeTests(unittest.TestCase):
    def test_prints_the_env_file_and_the_unit_and_changes_nothing(self):
        code, out, err = run("--device-id", "kitchen-pi", "--endpoint", "10.0.0.5:4317", "--print")
        self.assertEqual(code, 0, err)
        for line in ("DEVICE_ID=kitchen-pi", "OTLP_ENDPOINT=10.0.0.5:4317", "PUSH_INTERVAL=5", "DISK_PATH=/"):
            self.assertIn(line, out)
        for line in ("EnvironmentFile=/etc/device-agent.env", "Restart=always", "RestartSec=5",
                     "WantedBy=multi-user.target", "After=network-online.target",
                     "ExecStart=/opt/device-agent/venv/bin/python /opt/device-agent/device_agent.py"):
            self.assertIn(line, out)
        for path in ("/etc/device-agent.env", "/etc/systemd/system/device-agent.service"):
            self.assertFalse(os.path.exists(path), f"--print must not create {path}")

    def test_service_does_not_run_as_root(self):
        _, out, _ = run("--device-id", "a", "--endpoint", "h:1", "--print")
        self.assertIn("DynamicUser=yes", out)
        self.assertIn("NoNewPrivileges=yes", out)
        self.assertNotRegex(out, r"(?m)^User=root")

    def test_url_decoration_on_the_endpoint_is_tidied(self):
        _, out, _ = run("--device-id", "a", "--endpoint", "http://server.lan:4317/", "--print")
        self.assertIn("OTLP_ENDPOINT=server.lan:4317\n", out)

    def test_options_are_passed_through(self):
        _, out, _ = run("--device-id", "a", "--endpoint", "h:1", "--interval", "2.5", "--disk-path", "/mnt/data", "--print")
        self.assertIn("PUSH_INTERVAL=2.5\n", out)
        self.assertIn("DISK_PATH=/mnt/data\n", out)

    def test_every_env_line_is_plain_key_equals_value(self):
        # The values end up in a systemd EnvironmentFile: one KEY=value per line, nothing else.
        _, out, _ = run("--device-id", "a.b_c-1", "--endpoint", "my-host.local:4317", "--print")
        block = out.split("# /etc/systemd/system/device-agent.service")[0].splitlines()[1:]
        for line in filter(None, block):
            self.assertRegex(line, r"^[A-Z_]+=[A-Za-z0-9._/:-]+$")


@unittest.skipUnless(shutil.which("bash"), "bash is needed")
class ValidationTests(unittest.TestCase):
    GOOD = ("--endpoint", "h:4317", "--print")

    def assert_rejected(self, args, text):
        code, out, err = run(*args)
        self.assertNotEqual(code, 0, args)
        self.assertIn(text, err, args)
        self.assertEqual(out, "", f"nothing may be printed for rejected input: {args}")

    def test_device_id_rules_match_the_agent(self):
        for bad in ["Bad ID", "UPPER", "-lead", "has space", 'q"uote', "a" * 64, "x\nPUSH_INTERVAL=1", "a;b", "$(id)"]:
            self.assert_rejected(("--device-id", bad, *self.GOOD), "invalid --device-id")

    def test_the_same_rule_as_the_agent_itself(self):
        # One rule in two languages: keep them from drifting apart.
        import sys
        sys.path.insert(0, os.path.join(ROOT, "agent"))
        from device_agent import DEVICE_ID_RE
        for candidate in ["ok", "a.b", "a_b-c", "0x", "Bad", "-x", "a" * 63, "a" * 64, "x y", ""]:
            shell_ok = run("--device-id", candidate, *self.GOOD)[0] == 0 if candidate else False
            self.assertEqual(shell_ok, bool(DEVICE_ID_RE.fullmatch(candidate)), repr(candidate))

    def test_required_options(self):
        self.assert_rejected(("--endpoint", "h:1", "--print"), "--device-id is required")
        self.assert_rejected(("--device-id", "a", "--print"), "--endpoint is required")

    def test_endpoint_must_be_host_colon_port_and_plain_grpc(self):
        for bad in ["https://h:4317", "h", "h:", "h:port", "h:99999999", "h :1", "h:1;rm", "h:1\nX=1", ":4317"]:
            self.assert_rejected(("--device-id", "a", "--endpoint", bad, "--print"), "invalid --endpoint")

    def test_interval_must_be_a_positive_number(self):
        for bad in ["0", "0.0", "-1", "abc", "1e3", "", "5;x"]:
            self.assert_rejected(("--device-id", "a", "--endpoint", "h:1", "--interval", bad, "--print"), "invalid --interval")

    def test_disk_path_must_be_absolute_and_plain(self):
        for bad in ["relative", "/has space", "/a;b", "/x\nY=1"]:
            self.assert_rejected(("--device-id", "a", "--endpoint", "h:1", "--disk-path", bad, "--print"), "invalid --disk-path")

    def test_unknown_option_and_help(self):
        self.assert_rejected(("--frobnicate",), "unknown option")
        code, out, _ = run("--help")
        self.assertEqual(code, 0)
        self.assertIn("--uninstall", out)

    @unittest.skipIf(os.geteuid() == 0, "only meaningful as a normal user")
    def test_installing_needs_root(self):
        self.assert_rejected(("--device-id", "a", "--endpoint", "h:1"), "run with sudo")


@unittest.skipUnless(shutil.which("bash"), "bash is needed")
class FileTests(unittest.TestCase):
    def test_the_script_is_executable_and_has_a_shebang(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK))
        with open(SCRIPT) as f:
            self.assertEqual(f.readline().strip(), "#!/usr/bin/env bash")

    def test_no_windows_line_endings(self):
        # CRLF breaks bash scripts; a Windows editor can add them.
        with open(SCRIPT, "rb") as f:
            self.assertNotIn(b"\r", f.read())

    def test_uses_the_env_vars_the_agent_actually_reads(self):
        with open(os.path.join(ROOT, "agent", "device_agent.py")) as f:
            agent = f.read()
        _, out, _ = run("--device-id", "a", "--endpoint", "h:1", "--print")
        for name in re.findall(r"^([A-Z_]+)=", out.split("# /etc/systemd")[0], flags=re.M):
            self.assertIn(f'"{name}"', agent, f"the agent never reads {name}")


if __name__ == "__main__":
    unittest.main()
