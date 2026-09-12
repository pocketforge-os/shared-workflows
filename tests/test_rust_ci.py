#!/usr/bin/env python3
"""Fault controls at the actual admission/identity boundary; no task packages."""
import importlib.util
import contextlib
import queue
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("rust_ci", Path(__file__).parents[1] / "scripts/run-rust-ci.py")
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


class CapacityTests(unittest.TestCase):
    def fs(self, size=100 * ci.GIB, inodes=1000000):
        return SimpleNamespace(f_bavail=size // 4096, f_frsize=4096, f_favail=inodes)

    def test_bytes_and_inodes_fail_before_engine_or_deletion(self):
        for fs, reason in ((self.fs(size=16 * ci.GIB), "disk_capacity"),
                           (self.fs(inodes=299999), "inode_capacity")):
            with self.subTest(reason=reason), patch.object(ci.os, "statvfs", return_value=fs), \
                    patch.object(ci.subprocess, "run") as engine, patch.object(ci.shutil, "rmtree") as deletion:
                with self.assertRaisesRegex(ci.Refused, reason):
                    ci.capacity(["/docker", "/run-data"], 0, 32 * ci.GIB)
                engine.assert_not_called()
                deletion.assert_not_called()

    def test_outstanding_reservation_cannot_be_double_spent(self):
        with patch.object(ci.os, "statvfs", return_value=self.fs(size=24 * ci.GIB)):
            ci.capacity(["/run-data"], 0, 32 * ci.GIB)
            with self.assertRaisesRegex(ci.Refused, "disk_capacity"):
                ci.capacity(["/run-data"], 1, 32 * ci.GIB)

    def test_memory_floor_and_two_admissible_runs(self):
        with patch.object(ci.os, "statvfs", return_value=self.fs()):
            ci.capacity(["/run-data"], 1, 12 * ci.GIB)
            with self.assertRaisesRegex(ci.Refused, "memory_capacity"):
                ci.capacity(["/run-data"], 1, 9 * ci.GIB)

    def test_both_output_and_engine_filesystems_must_fit(self):
        with patch.object(ci.os, "statvfs", side_effect=[self.fs(), self.fs(size=ci.GIB)]):
            with self.assertRaisesRegex(ci.Refused, "disk_capacity"):
                ci.capacity(["/run-data", "/docker"], 0, 32 * ci.GIB)

    def test_wrong_sha_or_dirty_source_refuses(self):
        with patch.object(ci, "output", return_value="a" * 40):
            with self.assertRaisesRegex(ci.Refused, "source_identity"):
                ci.verify_source(Path("source"), "b" * 40)
        with patch.object(ci, "output", side_effect=["a" * 40, " M Cargo.lock"]):
            with self.assertRaisesRegex(ci.Refused, "dirty_source"):
                ci.verify_source(Path("source"), "a" * 40)

    def test_unpinned_image_never_reaches_engine(self):
        with patch.object(ci.subprocess, "run") as engine:
            with self.assertRaisesRegex(ci.Refused, "unpinned_image"):
                ci.run(Path("source"), "a" * 40, Path("platform"), "b" * 40,
                       "10.0.32.86:5555/pocketforge/ci-rust:latest", "ci.sh")
            engine.assert_not_called()


class SlotTests(unittest.TestCase):
    def test_busy_cohort_times_out_without_holding_spare_or_touching_sibling(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as cleanup:
            root = Path(tmp)
            marker = root / "sibling-cache"
            marker.write_text("keep")
            sibling = ci.acquire_slots(root)[0]
            cleanup.callback(sibling.close)
            with self.assertRaisesRegex(ci.Refused, "capacity_slots_timeout"):
                ci.acquire_slots(root, 2, wait_seconds=0.05)
            spare = ci.acquire_slots(root, wait_seconds=0)[0]
            cleanup.callback(spare.close)
            self.assertNotEqual(sibling.name, spare.name)
            self.assertEqual(marker.read_text(), "keep")

    def test_cohort_waits_then_acquires_both_and_ordinary_waits_for_release(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as cleanup:
            root = Path(tmp)
            sibling = ci.acquire_slots(root)[0]
            cleanup.callback(sibling.close)
            result = queue.Queue()
            waiting = threading.Event()

            def cohort():
                waiting.set()
                result.put(ci.acquire_slots(root, 2, wait_seconds=3))

            child = threading.Thread(target=cohort)
            child.start()
            self.assertTrue(waiting.wait(1))
            with self.assertRaises(queue.Empty):
                result.get(timeout=0.05)
            sibling.close()
            pair = result.get(timeout=3)
            child.join(timeout=1)
            for guard in pair:
                cleanup.callback(guard.close)
            with self.assertRaisesRegex(ci.Refused, "capacity_slots_timeout"):
                ci.acquire_slots(root, wait_seconds=0)
            for guard in pair:
                guard.close()
            normal = ci.acquire_slots(root, wait_seconds=0)[0]
            cleanup.callback(normal.close)

    def test_inherited_descriptor_must_be_a_real_exclusive_pool_slot(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as cleanup:
            root = Path(tmp)
            unrelated = cleanup.enter_context((root / "not-a-slot").open("a"))
            with self.assertRaisesRegex(ci.Refused, "invalid_inherited_slot"):
                ci.adopt_slot(root, unrelated.fileno())
            pair = ci.acquire_slots(root, 2, wait_seconds=0)
            for guard in pair:
                cleanup.callback(guard.close)
            inherited = ci.adopt_slot(root, pair[0].fileno())
            cleanup.callback(inherited.close)
            pair[0].close()
            with self.assertRaisesRegex(ci.Refused, "capacity_slots_timeout"):
                ci.acquire_slots(root, wait_seconds=0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
