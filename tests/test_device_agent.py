"""Unit tests for agent/device_agent.py. Stdlib only, no network.

Run from the repo root: python3 -m unittest discover -s tests -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

AGENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agent")
sys.path.insert(0, AGENT_DIR)
import device_agent as da  # noqa: E402

STAT_1 = "cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 50 0 50 350 50 0 0 0 0 0\nintr 1\n"
# +100 user, +100 system, +200 idle, +100 iowait since STAT_1 -> 200 busy of 500 total
STAT_2 = "cpu  200 0 200 900 200 0 0 0 0 0\ncpu0 100 0 100 450 100 0 0 0 0 0\nintr 2\n"

MEMINFO = """MemTotal:        1000000 kB
MemFree:          100000 kB
MemAvailable:     250000 kB
Buffers:           50000 kB
Cached:           200000 kB
SwapTotal:             0 kB
"""


class CpuTests(unittest.TestCase):
    def test_parse_counts_idle_and_iowait_as_idle(self):
        self.assertEqual(da.parse_cpu_times(STAT_1), (200, 1000))  # 200 busy of 1000

    def test_delta_between_samples(self):
        prev, cur = da.parse_cpu_times(STAT_1), da.parse_cpu_times(STAT_2)
        self.assertAlmostEqual(da.cpu_percent(prev, cur), 40.0)  # 200 busy / 500 total

    def test_fully_idle_and_fully_busy(self):
        self.assertEqual(da.cpu_percent((10, 100), (10, 200)), 0.0)
        self.assertEqual(da.cpu_percent((10, 100), (110, 200)), 100.0)

    def test_no_elapsed_time_or_counter_reset_gives_none(self):
        self.assertIsNone(da.cpu_percent((10, 100), (10, 100)))
        self.assertIsNone(da.cpu_percent((10, 100), (5, 50)))

    def test_clamped_to_0_100(self):
        self.assertEqual(da.cpu_percent((10, 100), (500, 200)), 100.0)
        self.assertEqual(da.cpu_percent((100, 100), (50, 200)), 0.0)

    def test_old_kernel_with_four_fields(self):
        self.assertEqual(da.parse_cpu_times("cpu  10 0 10 80\n"), (20, 100))

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            da.parse_cpu_times("intr 1 2 3\n")
        with self.assertRaises(ValueError):
            da.parse_cpu_times("cpu  1 2\n")
        with self.assertRaises(ValueError):
            da.parse_cpu_times("cpu  a b c d\n")


class MemoryDiskUptimeTests(unittest.TestCase):
    def test_meminfo_uses_memavailable(self):
        self.assertAlmostEqual(da.parse_meminfo_percent(MEMINFO), 75.0)

    def test_meminfo_fallback_without_memavailable(self):
        text = "\n".join(l for l in MEMINFO.splitlines() if not l.startswith("MemAvailable"))
        # available = 100000 + 50000 + 200000 = 350000 -> 65% used
        self.assertAlmostEqual(da.parse_meminfo_percent(text), 65.0)

    def test_meminfo_missing_total_raises(self):
        with self.assertRaises(ValueError):
            da.parse_meminfo_percent("MemFree: 1 kB\n")

    def test_uptime(self):
        self.assertAlmostEqual(da.parse_uptime("12345.67 54321.00\n"), 12345.67)
        with self.assertRaises(ValueError):
            da.parse_uptime("garbage")

    def test_disk_percent_matches_df_formula(self):
        # used = 100 - 40 = 60 blocks, avail = 30 blocks (10 reserved for root) -> 60 / 90
        st = SimpleNamespace(f_blocks=100, f_bfree=40, f_bavail=30, f_frsize=4096)
        self.assertAlmostEqual(da.disk_percent(st), 100.0 * 60 / 90)

    def test_disk_without_capacity_raises(self):
        with self.assertRaises(ValueError):
            da.disk_percent(SimpleNamespace(f_blocks=0, f_bfree=0, f_bavail=0, f_frsize=4096))


class TemperatureTests(unittest.TestCase):
    def test_hottest_zone_wins(self):
        self.assertEqual(da.hottest_temperature(["42000\n", "55500\n", "30000\n"]), 55.5)

    def test_out_of_range_ignored(self):
        self.assertEqual(da.hottest_temperature(["999999\n", "-90000\n", "41000\n"]), 41.0)
        self.assertIsNone(da.hottest_temperature(["999999\n", "-90000\n"]))
        self.assertEqual(da.hottest_temperature(["150000", "-40000"]), 150.0)  # bounds inclusive

    def test_none_when_unavailable_or_unparseable(self):
        self.assertIsNone(da.hottest_temperature([]))
        self.assertIsNone(da.hottest_temperature(["", "n/a\n"]))

    def test_read_temperature_from_files(self):
        with tempfile.TemporaryDirectory() as root:
            for name, raw in (("thermal_zone0", "47000\n"), ("thermal_zone1", "61000\n"),
                              ("thermal_zone2", "oops\n"), ("thermal_zone3", "250000\n")):
                os.makedirs(os.path.join(root, name))
                with open(os.path.join(root, name, "temp"), "w") as f:
                    f.write(raw)
            self.assertEqual(da.read_temperature(os.path.join(root, "thermal_zone*", "temp")), 61.0)

    def test_read_temperature_no_zones_returns_none(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(da.read_temperature(os.path.join(root, "thermal_zone*", "temp")))

    def test_unreadable_zone_is_skipped(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "thermal_zone0", "temp"))  # a directory: open() fails
            self.assertIsNone(da.read_temperature(os.path.join(root, "thermal_zone*", "temp")))


class DeviceIdTests(unittest.TestCase):
    def test_valid(self):
        for ok in ("a", "demo-pi-1", "test-a-1", "0abc", "a.b_c-d", "x" * 63):
            self.assertTrue(da.is_valid_device_id(ok), ok)

    def test_invalid(self):
        for bad in ("", "Demo", "-a", ".a", "_a", "a b", "a/b", "a\n", "x" * 64, "café", "a:b"):
            self.assertFalse(da.is_valid_device_id(bad), repr(bad))


class ConfigTests(unittest.TestCase):
    def cfg(self, argv=(), env=None, hostname="Host-One"):
        with mock.patch.object(da.socket, "gethostname", return_value=hostname):
            return da.parse_config(list(argv), env or {})

    def test_defaults(self):
        c = self.cfg()
        self.assertEqual((c.device_id, c.endpoint, c.interval, c.disk_path, c.dry_run),
                         ("host-one", "localhost:4317", 5.0, "/", False))

    def test_env_beats_default(self):
        c = self.cfg(env={"DEVICE_ID": "env-dev", "OTLP_ENDPOINT": "envhost:1", "PUSH_INTERVAL": "7",
                          "DISK_PATH": "/data"})
        self.assertEqual((c.device_id, c.endpoint, c.interval, c.disk_path),
                         ("env-dev", "envhost:1", 7.0, "/data"))

    def test_flag_beats_env(self):
        c = self.cfg(["--device-id", "flag-dev", "--endpoint", "flaghost:2", "--interval", "2.5",
                      "--disk-path", "/mnt"],
                     env={"DEVICE_ID": "env-dev", "OTLP_ENDPOINT": "envhost:1", "PUSH_INTERVAL": "7",
                          "DISK_PATH": "/data"})
        self.assertEqual((c.device_id, c.endpoint, c.interval, c.disk_path),
                         ("flag-dev", "flaghost:2", 2.5, "/mnt"))

    def test_empty_env_counts_as_unset(self):
        self.assertEqual(self.cfg(env={"DEVICE_ID": "", "PUSH_INTERVAL": ""}).device_id, "host-one")

    def test_endpoint_scheme_handling(self):
        self.assertEqual(self.cfg(["--endpoint", "http://c:4317/"]).endpoint, "c:4317")
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            self.cfg(["--endpoint", "https://c:4317"])

    def test_dry_run_flag(self):
        self.assertTrue(self.cfg(["--dry-run"]).dry_run)

    def test_invalid_values_exit_nonzero(self):
        bad = [(["--device-id", "Bad ID"], {}), ([], {"DEVICE_ID": "UPPER"}),
               (["--interval", "0"], {}), (["--interval", "abc"], {}), (["--interval", "nan"], {}),
               (["--interval", "inf"], {}), ([], {"PUSH_INTERVAL": "-1"})]
        for argv, env in bad:
            with self.subTest(argv=argv, env=env), mock.patch("sys.stderr"):
                with self.assertRaises(SystemExit) as cm:
                    self.cfg(argv, env)
                self.assertNotEqual(cm.exception.code, 0)

    def test_default_hostname_is_validated_too(self):
        self.assertEqual(self.cfg(hostname="My_Host.Local").device_id, "my_host.local")
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            self.cfg(hostname="-weird")


class SamplerTests(unittest.TestCase):
    def test_bad_disk_path_drops_only_that_metric_and_logs_once(self):
        with tempfile.TemporaryDirectory() as empty:
            s = da.Sampler("/definitely/not/a/path", thermal_glob=os.path.join(empty, "z*", "temp"))
            with self.assertLogs("device-agent", level="WARNING") as logs:
                first, second = s.snapshot(), s.snapshot()
        for snap in (first, second):
            self.assertNotIn("device_disk_utilization_percent", snap)
            self.assertNotIn("device_temperature_celsius", snap)  # no sensor: no data point
            self.assertIn("device_uptime_seconds", snap)
            self.assertIn("device_memory_utilization_percent", snap)
        self.assertEqual(len(logs.records), 1)

    def test_values_in_range_when_everything_works(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "thermal_zone0"))
            with open(os.path.join(root, "thermal_zone0", "temp"), "w") as f:
                f.write("48500\n")
            s = da.Sampler("/", thermal_glob=os.path.join(root, "thermal_zone*", "temp"))
            snap = s.snapshot()
        self.assertEqual(snap["device_temperature_celsius"], 48.5)
        for name in ("device_memory_utilization_percent", "device_disk_utilization_percent"):
            self.assertTrue(0 <= snap[name] <= 100, name)
        self.assertGreater(snap["device_uptime_seconds"], 0)
        self.assertTrue(set(snap) <= set(da.METRICS))


class CliTests(unittest.TestCase):
    script = os.path.join(AGENT_DIR, "device_agent.py")

    def run_agent(self, *args, env=None):
        full_env = {k: v for k, v in os.environ.items()
                    if k not in ("DEVICE_ID", "OTLP_ENDPOINT", "PUSH_INTERVAL", "DISK_PATH")}
        full_env.update(env or {})
        return subprocess.run([sys.executable, self.script, *args], capture_output=True, text=True,
                              timeout=30, env=full_env)

    def test_dry_run_prints_one_json_snapshot(self):
        r = self.run_agent("--dry-run", "--device-id", "test-unit-1", "--endpoint", "127.0.0.1:1")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data["labels"]["device_id"], "test-unit-1")
        self.assertEqual(data["resource"], {"service.name": "device-agent",
                                            "service.instance.id": "test-unit-1"})
        self.assertIn("device_uptime_seconds", data["metrics"])

    def test_invalid_device_id_exits_nonzero_with_message(self):
        r = self.run_agent("--dry-run", "--device-id", "Not Valid")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("invalid device id", r.stderr)
        self.assertEqual(r.stdout, "")


if __name__ == "__main__":
    unittest.main()
