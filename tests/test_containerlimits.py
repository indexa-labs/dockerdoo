"""Unit tests for resources/containerlimits.py, which need neither Docker nor Odoo."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "resources"))

import containerlimits as limits  # noqa: E402

MiB = limits.MiB


class MemoryLimit(unittest.TestCase):
    def read(self, cgroup=(), meminfo_kib=None):
        with tempfile.TemporaryDirectory() as directory:
            files = []
            for index, text in enumerate(cgroup):
                path = Path(directory, f"cgroup{index}")
                path.write_text(text)
                files.append(path)
            meminfo = Path(directory, "meminfo")
            meminfo.write_text(f"MemTotal:       {meminfo_kib} kB\nMemFree: 1 kB\n" if meminfo_kib else "")
            return limits.memory_limit(files, meminfo)

    def test_cgroup_limit_below_machine_memory(self):
        self.assertEqual(self.read(["536870912\n"], 16 * 1024 * 1024), 512 * MiB)

    def test_unlimited_cgroup_falls_back_to_machine_memory(self):
        self.assertEqual(self.read(["max\n"], 2 * 1024 * 1024), 2048 * MiB)
        # cgroup v1 reports "no limit" as a number larger than any machine
        self.assertEqual(self.read(["9223372036854771712\n"], 2 * 1024 * 1024), 2048 * MiB)

    def test_unknown(self):
        self.assertIsNone(self.read())


class Derive(unittest.TestCase):
    def test_threads_follow_memory_within_bounds(self):
        threads = lambda mib: int(limits.derive({}, mib * MiB)["ODOO_MAX_HTTP_THREADS"])  # noqa: E731
        self.assertEqual([threads(256), threads(1024), threads(2048), threads(8192)], [32, 64, 128, 256])

    def test_limits_leave_room_for_thread_stacks_and_other_processes(self):
        derived = limits.derive({}, 1024 * MiB)
        stacks = 64 * limits.THREAD_STACK
        self.assertEqual(int(derived["ODOO_LIMIT_MEMORY_SOFT"]), int(1024 * MiB * 0.70) + stacks)
        self.assertEqual(int(derived["ODOO_LIMIT_MEMORY_HARD"]), int(1024 * MiB * 0.90) + stacks)

    def test_explicit_settings_win(self):
        derived = limits.derive({"ODOO_MAX_HTTP_THREADS": "10", "ODOO_LIMIT_MEMORY_HARD": "1"}, 1024 * MiB)
        self.assertNotIn("ODOO_MAX_HTTP_THREADS", derived)
        self.assertNotIn("ODOO_LIMIT_MEMORY_HARD", derived)
        self.assertEqual(int(derived["ODOO_LIMIT_MEMORY_SOFT"]), int(1024 * MiB * 0.70) + 10 * limits.THREAD_STACK)

    def test_empty_thread_setting_counts_as_unset(self):
        self.assertEqual(limits.derive({"ODOO_MAX_HTTP_THREADS": ""}, 1024 * MiB)["ODOO_MAX_HTTP_THREADS"], "64")

    def test_worker_processes_keep_odoo_memory_limits(self):
        self.assertEqual(set(limits.derive({"ODOO_WORKERS": "2"}, 1024 * MiB)), {"ODOO_MAX_HTTP_THREADS"})

    def test_nothing_without_a_limit_or_on_a_dedicated_host(self):
        self.assertEqual(limits.derive({}, None), {})
        self.assertEqual(limits.derive({}, limits.DEDICATED_HOST), {})
        self.assertNotEqual(limits.derive({}, limits.DEDICATED_HOST - MiB), {})


if __name__ == "__main__":
    unittest.main()
