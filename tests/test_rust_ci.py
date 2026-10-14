#!/usr/bin/env python3
"""Fault controls at the actual admission/identity boundary; no task packages."""
import importlib.util
import contextlib
import json
import multiprocessing
import os
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("rust_ci", Path(__file__).parents[1] / "scripts/run-rust-ci.py")
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)

CI_RUST_IMAGE = "10.0.32.86:5555/pocketforge/ci-rust@sha256:801f15f7e8fd20b449e3b0d0a7a56d632e28dae825b251a19a30e6f8b32bd8e9"
CI_TOOLS_IMAGE = "10.0.32.86:5555/pocketforge/ci-tools@sha256:2ff536627228c90e5c32b7d6345b21600a38a17481c075b41683db47ead3e862"


def hold_lock(path, ready):
    with Path(path).open("a") as guard:
        ci.fcntl.flock(guard, ci.fcntl.LOCK_EX | ci.fcntl.LOCK_NB)
        ready.set()
        signal.pause()


class CapacityTests(unittest.TestCase):
    def reservation(self, root, name, *, lock=True):
        directory = root / name
        directory.mkdir()
        (directory / "reservation.json").write_text(json.dumps({"name": f"container-{name}"}))
        guard = (directory / "run.lock").open("a") if lock else None
        return directory, guard

    def test_dead_owner_reclaimed_by_its_recorded_name_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dead, dead_guard = self.reservation(root, "run-dead")
            dead_guard.close()
            ready = multiprocessing.Event()
            owner = multiprocessing.Process(target=hold_lock, args=(dead / "run.lock", ready))
            owner.start()
            self.assertTrue(ready.wait(2))
            os.kill(owner.pid, signal.SIGKILL)
            owner.join(2)
            self.assertEqual(owner.exitcode, -signal.SIGKILL)
            live, live_guard = self.reservation(root, "run-live")
            ci.fcntl.flock(live_guard, ci.fcntl.LOCK_EX | ci.fcntl.LOCK_NB)
            try:
                with patch.object(ci.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as engine:
                    self.assertEqual(ci.active_reservations(root), 1)
                engine.assert_called_once_with(["docker", "rm", "-f", "container-run-dead"], timeout=20,
                                               stdout=ci.subprocess.DEVNULL, stderr=ci.subprocess.DEVNULL)
                self.assertFalse(dead.exists())
                self.assertTrue(live.exists())
            finally:
                live_guard.close()

    def test_dead_owner_without_container_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dead, guard = self.reservation(root, "run-never-created")
            guard.close()
            absent = SimpleNamespace(returncode=1,
                                     stderr="Error: No such container: container-run-never-created\n")
            with patch.object(ci.subprocess, "run",
                              side_effect=[SimpleNamespace(returncode=1), absent]) as engine:
                self.assertEqual(ci.active_reservations(root), 0)
            self.assertEqual(engine.call_count, 2)
            self.assertFalse(dead.exists())

    def test_failed_create_without_allocation_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dead, guard = self.reservation(root, "run-create-failed")
            guard.close()
            absent = SimpleNamespace(returncode=1,
                                     stderr="Error: No such container: container-run-create-failed\n")
            with patch.object(ci.subprocess, "run",
                              side_effect=[SimpleNamespace(returncode=125), absent]):
                self.assertEqual(ci.active_reservations(root), 0)
            self.assertFalse(dead.exists())

    def test_engine_failure_retains_dead_reservation_and_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dead, guard = self.reservation(root, "run-engine-failed")
            guard.close()
            unavailable = SimpleNamespace(returncode=1,
                                          stderr="Cannot connect to the Docker daemon\n")
            with patch.object(ci.subprocess, "run",
                              side_effect=[SimpleNamespace(returncode=1), unavailable]), \
                    patch("builtins.print") as log:
                self.assertEqual(ci.active_reservations(root), 1)
            self.assertTrue(dead.exists())
            event = json.loads(log.call_args.args[0])
            self.assertEqual(event["reason"], "engine_cleanup_unconfirmed")

    def test_engine_timeout_retains_dead_reservation_and_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dead, guard = self.reservation(root, "run-engine-timeout")
            guard.close()
            with patch.object(ci.subprocess, "run",
                              side_effect=ci.subprocess.TimeoutExpired("docker", 20)), \
                    patch("builtins.print") as log:
                self.assertEqual(ci.active_reservations(root), 1)
            self.assertTrue(dead.exists())
            event = json.loads(log.call_args.args[0])
            self.assertEqual(event["reason"], "reclaim_unconfirmed")

    def test_live_owner_and_same_label_sibling_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live, guard = self.reservation(root, "run-live")
            ci.fcntl.flock(guard, ci.fcntl.LOCK_EX | ci.fcntl.LOCK_NB)
            try:
                with patch.object(ci.subprocess, "run") as engine:
                    self.assertEqual(ci.active_reservations(root), 1)
                engine.assert_not_called()
                self.assertTrue(live.exists())
            finally:
                guard.close()

    def test_missing_run_lock_with_existing_container_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy, _ = self.reservation(root, "run-legacy", lock=False)
            present = SimpleNamespace(returncode=0, stderr="")
            with patch("builtins.print") as log, \
                    patch.object(ci.subprocess, "run", return_value=present) as engine:
                self.assertEqual(ci.active_reservations(root), 1)
            engine.assert_called_once_with(
                ["docker", "container", "inspect", "container-run-legacy"], timeout=20,
                stdout=ci.subprocess.DEVNULL, stderr=ci.subprocess.PIPE, text=True)
            self.assertTrue(legacy.exists())
            event = json.loads(log.call_args.args[0])
            self.assertEqual(event["reason"], "missing_run_lock")

    def test_legacy_reservation_without_lock_or_container_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy, _ = self.reservation(root, "run-legacy", lock=False)
            absent = SimpleNamespace(
                returncode=1, stderr="Error: No such container: container-run-legacy\n")
            with patch("builtins.print") as log, \
                    patch.object(ci.subprocess, "run", return_value=absent):
                self.assertEqual(ci.active_reservations(root), 0)
            self.assertFalse(legacy.exists())
            event = json.loads(log.call_args.args[0])
            self.assertEqual(event["reason"], "legacy_owner_gone")

    def test_lockless_reservation_is_preserved_while_legacy_slot_owner_lives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy, _ = self.reservation(root, "run-legacy", lock=False)
            owner = ci.acquire_slots(root, wait_seconds=0)[0]
            try:
                with patch("builtins.print") as log, \
                        patch.object(ci.subprocess, "run") as engine:
                    self.assertEqual(ci.active_reservations(root), 1)
                engine.assert_not_called()
                self.assertTrue(legacy.exists())
                event = json.loads(log.call_args.args[0])
                self.assertEqual(event["reason"], "legacy_owner_slot_alive")
            finally:
                owner.close()

    def test_caller_slot_is_not_mistaken_for_legacy_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy, _ = self.reservation(root, "run-legacy", lock=False)
            caller = ci.acquire_slots(root, wait_seconds=0)[0]
            absent = SimpleNamespace(
                returncode=1, stderr="Error: No such container: container-run-legacy\n")
            try:
                with patch.object(ci.subprocess, "run", return_value=absent):
                    self.assertEqual(ci.active_reservations(root, owned_slot=caller), 0)
            finally:
                caller.close()
            self.assertFalse(legacy.exists())

    def test_live_disk_scan_tolerates_unlinked_rustc_intermediate_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            keep = root / "sibling-output"
            keep.write_bytes(b"keep" * 1024)
            transient = root / "rustc.rcgu.o"
            transient.write_bytes(b"object" * 1024)
            original = Path.lstat

            def racing_stat(path):
                if path == transient:
                    path.unlink()  # Gone after readdir, before stat, as in real CI.
                return original(path)

            with patch.object(Path, "lstat", racing_stat):
                self.assertGreaterEqual(ci.disk_usage(root), keep.stat().st_blocks * 512)
            self.assertEqual(keep.read_bytes(), b"keep" * 1024)
            self.assertFalse(transient.exists())
            with patch.object(Path, "lstat", side_effect=PermissionError("unreadable")):
                with self.assertRaisesRegex(ci.Refused, "run_disk_usage_unknown"):
                    ci.disk_usage(root)
            with self.assertRaisesRegex(ci.Refused, "run_disk_usage_unknown"):
                ci.disk_usage(root / "missing-run")

    def test_reservation_is_locked_before_atomic_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory, guard, reservation = ci.publish_reservation(
                root, {"disk": ci.DISK, "memory": ci.MEMORY})
            try:
                self.assertTrue(directory.name.startswith("run-"))
                self.assertEqual(list(root.glob(".run-*")), [])
                self.assertEqual(json.loads((directory / "reservation.json").read_text()),
                                 reservation)
                contender = (directory / "run.lock").open("a")
                try:
                    with self.assertRaises(BlockingIOError):
                        ci.fcntl.flock(contender, ci.fcntl.LOCK_EX | ci.fcntl.LOCK_NB)
                finally:
                    contender.close()
            finally:
                guard.close()

    def test_cleanup_unpublishes_reservation_before_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory, guard = self.reservation(root, "run-owned")
            ci.fcntl.flock(guard, ci.fcntl.LOCK_EX | ci.fcntl.LOCK_NB)
            removed = []
            unlink = Path.unlink

            def tracked_unlink(path, *args, **kwargs):
                removed.append(path.name)
                return unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", tracked_unlink):
                ci.cleanup_reservation(directory, guard)
            self.assertEqual(removed[:2], ["reservation.json", "run.lock"])
            self.assertFalse(directory.exists())

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

    def test_image_allowlist_accepts_two_exact_digests_only(self):
        for image in (CI_RUST_IMAGE, CI_TOOLS_IMAGE):
            with self.subTest(image=image):
                ci.verify_image(image)

        refused = (
            CI_RUST_IMAGE[:-1] + "0",
            CI_TOOLS_IMAGE[:-1] + "0",
            "10.0.32.86:5555/pocketforge/ci-tools:latest",
            "10.0.32.86:5555/pocketforge/other@sha256:" + "a" * 64,
        )
        for image in refused:
            with self.subTest(image=image), self.assertRaisesRegex(
                ci.Refused, "unpinned_image"
            ):
                ci.verify_image(image)

    def test_workflow_and_controller_image_allowlists_match(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/rust-container.yml").read_text()
        workflow_images = set(
            re.findall(
                r"10\.0\.32\.86:5555/pocketforge/[a-z-]+@sha256:[0-9a-f]{64}",
                workflow,
            )
        )
        self.assertEqual(workflow_images, ci.ALLOWED_IMAGES)


class CapacityWaitTests(unittest.TestCase):
    """A capacity refusal while siblings are active is contention: wait, never refuse."""

    def fs(self, size=100 * ci.GIB, inodes=1000000):
        return SimpleNamespace(f_bavail=size // 4096, f_frsize=4096, f_favail=inodes)

    def live_reservation(self, root, name):
        directory = root / name
        directory.mkdir()
        (directory / "reservation.json").write_text(json.dumps({"name": f"container-{name}"}))
        guard = (directory / "run.lock").open("a")
        ci.fcntl.flock(guard, ci.fcntl.LOCK_EX | ci.fcntl.LOCK_NB)
        return directory, guard

    def fake_reserve(self, calls, *, memory=8 * ci.GIB, fs=None):
        """Stand-in for the real reservation step: same capacity verdict, no engine."""
        def reserve(active):
            with patch.object(ci.os, "statvfs", return_value=fs or self.fs()):
                ci.capacity(["/run-data"], active, memory)
            calls.append(active)
            return {"name": "fake-reservation"}
        return reserve

    def events(self, log):
        for call in log.call_args_list:
            if call.args and isinstance(call.args[0], str) and call.args[0].startswith("{"):
                yield json.loads(call.args[0])

    def waiting_lines(self, log):
        return [e for e in self.events(log) if e.get("rust_ci") == "waiting_for_capacity"]

    def await_admission(self, outcome, timeout=10):
        got = outcome.get(timeout=timeout)
        if isinstance(got, BaseException):
            self.fail(f"admit raised instead of waiting: {got!r}")
        return got

    def test_refusal_while_active_waits_then_admits_once_the_sibling_clears(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as cleanup:
            root = Path(tmp)
            busy, busy_guard = self.live_reservation(root, "run-busy")
            cleanup.callback(busy_guard.close)
            calls = []
            outcome = queue.Queue()
            log = cleanup.enter_context(patch("builtins.print"))

            def waiter():
                try:
                    with patch.object(ci.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
                        outcome.put(ci.admit(root, self.fake_reserve(calls), wait_seconds=30))
                except BaseException as exc:
                    outcome.put(exc)

            child = threading.Thread(target=waiter)
            child.start()
            cleanup.callback(child.join, 5)
            # While the sibling reservation is live: neither admitted nor refused.
            with self.assertRaises(queue.Empty):
                outcome.get(timeout=0.3)
            self.assertEqual(calls, [])
            waiting = self.waiting_lines(log)
            self.assertEqual(len(waiting), 1, "waiting_for_capacity must be announced exactly once")
            self.assertEqual(waiting[0]["reason"], "memory_capacity")
            busy_guard.close()  # The sibling finishes; its reservation becomes reclaimable.
            slot, reservation = self.await_admission(outcome)
            cleanup.callback(slot.close)
            self.assertEqual(reservation["name"], "fake-reservation")
            self.assertEqual(calls, [0])  # admitted only after active dropped to zero
            self.assertFalse(busy.exists())  # dead sibling reclaimed by recorded name

    def test_without_active_reservations_insufficient_memory_refuses_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = []
            with patch("builtins.print") as log:
                started = time.monotonic()
                with self.assertRaisesRegex(ci.Refused, "memory_capacity"):
                    ci.admit(root, self.fake_reserve(calls, memory=4 * ci.GIB), wait_seconds=30)
                elapsed = time.monotonic() - started
            self.assertEqual(calls, [])
            self.assertLess(elapsed, 1, "active == 0 must refuse at once despite a long deadline")
            self.assertEqual(self.waiting_lines(log), [])
            spare = ci.acquire_slots(root, wait_seconds=0)[0]  # refusal released the slot
            spare.close()

    def test_expired_deadline_refuses_with_the_original_reason(self):
        for memory, fs, reason in ((8 * ci.GIB, None, "memory_capacity"),
                                   (32 * ci.GIB, self.fs(size=16 * ci.GIB), "disk_capacity"),
                                   (32 * ci.GIB, self.fs(inodes=299999), "inode_capacity")):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                busy, busy_guard = self.live_reservation(root, "run-busy")
                calls = []
                try:
                    with patch("builtins.print") as log:
                        started = time.monotonic()
                        with self.assertRaisesRegex(ci.Refused, reason):
                            ci.admit(root, self.fake_reserve(calls, memory=memory, fs=fs),
                                     wait_seconds=0.3)
                        elapsed = time.monotonic() - started
                    self.assertLess(elapsed, 5)
                    waiting = self.waiting_lines(log)
                    self.assertEqual(len(waiting), 1)
                    self.assertEqual(waiting[0]["reason"], reason)
                finally:
                    busy_guard.close()
                self.assertEqual(calls, [])

    def test_slot_and_admission_lock_are_released_while_waiting(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as cleanup:
            root = Path(tmp)
            busy, busy_guard = self.live_reservation(root, "run-busy")
            cleanup.callback(busy_guard.close)
            calls = []
            outcome = queue.Queue()
            log = cleanup.enter_context(patch("builtins.print"))

            def waiter():
                try:
                    with patch.object(ci.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
                        outcome.put(ci.admit(root, self.fake_reserve(calls), wait_seconds=60))
                except BaseException as exc:
                    outcome.put(exc)

            child = threading.Thread(target=waiter)
            child.start()
            cleanup.callback(child.join, 5)
            announced = time.monotonic() + 10
            while not self.waiting_lines(log):
                if time.monotonic() > announced:
                    self.fail("waiting_for_capacity was never announced")
                time.sleep(0.02)
            # The waiter is mid-wait, so BOTH slots plus the shared admission lock
            # are free: a sibling cohort acquires them without waiting it out.
            pair = ci.acquire_slots(root, 2, wait_seconds=1)
            for guard in pair:
                cleanup.callback(guard.close)
            with self.assertRaises(queue.Empty):
                outcome.get(timeout=0.3)  # still waiting, holding nothing
            for guard in pair:
                guard.close()
            busy_guard.close()
            slot, reservation = self.await_admission(outcome)
            cleanup.callback(slot.close)
            self.assertEqual(reservation["name"], "fake-reservation")
            self.assertEqual(calls, [0])


class RunAdmissionTests(unittest.TestCase):
    def test_admitted_run_reaches_execution_and_cleans_up_with_fakes_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir()
            sha = "a" * 40
            image = CI_RUST_IMAGE
            calls = []

            def fake_output(*args):
                calls.append(args)
                if "rev-parse" in args:
                    return sha
                if "status" in args:
                    return ""
                if "info" in args:
                    return "/docker"
                if "show" in args:
                    return "0"
                raise AssertionError(args)

            def fake_run(command, **kwargs):
                calls.append(tuple(command))
                return SimpleNamespace(returncode=0)

            child = SimpleNamespace(returncode=0, poll=lambda: 0, wait=lambda timeout=None: 0,
                                    stdout=iter(()))
            with patch.object(ci, "output", side_effect=fake_output), \
                    patch.object(ci.subprocess, "run", side_effect=fake_run), \
                    patch.object(ci.subprocess, "Popen", return_value=child), \
                    patch.object(ci.os, "statvfs", return_value=SimpleNamespace(
                        f_bavail=100 * ci.GIB // 4096, f_frsize=4096, f_favail=1000000)), \
                    patch.object(ci, "host_memory", return_value=32 * ci.GIB), \
                    patch("builtins.print") as log:
                rc = ci.run(root / "source", sha, root / "platform", sha, image, "ci.sh", root=state)
            self.assertEqual(rc, 0)
            kinds = [json.loads(c.args[0])["rust_ci"] for c in log.call_args_list
                     if c.args and isinstance(c.args[0], str) and c.args[0].startswith("{")]
            self.assertEqual(kinds, ["starting", "finished", "disk_accounting"])
            self.assertEqual(list(state.glob("run-*")), [])
            self.assertTrue(any(list(command[:3]) == ["docker", "rm", "-f"] for command in calls))
            create = next(command for command in calls if command[:2] == ("docker", "create"))
            self.assertIn("CARGO_INCREMENTAL=0", create)
            self.assertIn("CARGO_PROFILE_DEV_DEBUG=0", create)
            self.assertIn("CARGO_PROFILE_TEST_DEBUG=0", create)

    def test_accounting_emitted_for_pass_and_over_budget_refusal(self):
        """One hermetic invocation proves both the positive and negative controls."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sha = "a" * 40

            def fake_output(*args):
                if "rev-parse" in args:
                    return sha
                if "status" in args:
                    return ""
                if "info" in args:
                    return "/docker"
                if "show" in args:
                    return "0"
                raise AssertionError(args)

            def fake_run(command, **_kwargs):
                return SimpleNamespace(returncode=0)

            class Child:
                returncode = 0
                stdout = iter(("::group::compile\n", "::endgroup::\n"))

                def __init__(self, polls):
                    self.polls = iter(polls)

                def poll(self):
                    return next(self.polls, self.returncode)

                def wait(self, timeout=None):
                    return self.returncode

            def exercise(name, samples, polls, budget):
                state = root / name
                state.mkdir()
                child = Child(polls)
                readings = iter(samples)
                last = samples[-1]

                def disk_sample(_directory):
                    return next(readings, last)

                with patch.object(ci, "output", side_effect=fake_output), \
                        patch.object(ci.subprocess, "run", side_effect=fake_run), \
                        patch.object(ci.subprocess, "Popen", return_value=child), \
                        patch.object(ci.os, "statvfs", return_value=SimpleNamespace(
                            f_bavail=100 * ci.GIB // 4096, f_frsize=4096,
                            f_favail=1000000)), \
                        patch.object(ci, "host_memory", return_value=32 * ci.GIB), \
                        patch.object(ci, "disk_usage", side_effect=disk_sample), \
                        patch.object(ci.time, "sleep"), \
                        patch("builtins.print") as log:
                    outcome = None
                    try:
                        outcome = ci.run(root / "source", sha, root / "platform", sha,
                                         CI_RUST_IMAGE, "ci.sh", root=state,
                                         disk_budget=budget)
                    except ci.Refused as exc:
                        outcome = str(exc)
                events = [json.loads(call.args[0]) for call in log.call_args_list
                          if call.args and isinstance(call.args[0], str)
                          and call.args[0].startswith("{")]
                accounting = [event for event in events
                              if event.get("rust_ci") == "disk_accounting"]
                self.assertEqual(len(accounting), 1)
                return outcome, accounting[0]

            passed, pass_accounting = exercise("pass", [40, 40, 40], [0], 100)
            refused, refuse_accounting = exercise("refuse", [101], [None], 100)

            self.assertEqual(passed, 0)
            self.assertEqual(pass_accounting["peak_private_bytes"], 40)
            self.assertEqual(pass_accounting["phases"], [
                {"phase": "preflight", "peak_private_bytes": 40},
                {"phase": "compile", "peak_private_bytes": 40},
            ])
            self.assertEqual(refused, "run_disk_budget")
            self.assertGreater(refuse_accounting["peak_private_bytes"], 100)
            self.assertEqual(refuse_accounting["budget_bytes"], 100)


class RefusalReportingTests(unittest.TestCase):
    def test_every_refusal_reports_active_reservations_and_available_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for number in range(2):
                directory = root / f"run-{number}"
                directory.mkdir()
                (directory / "reservation.json").write_text("{}")
            with patch.object(ci, "host_memory", return_value=123456):
                event = ci.refusal_event(ci.Refused("memory_capacity"), root)
            self.assertEqual(event, {
                "rust_ci": "refused",
                "reason": "memory_capacity",
                "active": 2,
                "available_memory": 123456,
            })


class SlotTests(unittest.TestCase):
    def test_synthetic_fixture_owners_serialize_without_locking_ordinary_slots(self):
        import slot_contention
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as cleanup:
            root = Path(tmp)
            cleanup.enter_context(slot_contention.fixture_admission(root, wait_seconds=0))
            with self.assertRaisesRegex(slot_contention.live.ci.Refused, "control_fixture_timeout"):
                with slot_contention.fixture_admission(root, wait_seconds=0):
                    self.fail("another parked-fixture owner was admitted")
            ordinary = ci.acquire_slots(root, 2, wait_seconds=0)
            for guard in ordinary:
                cleanup.callback(guard.close)

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


LIVE_STUB = '''\
"""Hermetic stub of live_rust_ci: fakes git/docker, keeps the real ci module."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import time

CONTROLLER = Path(__file__).parent / "controller.py"
_spec = importlib.util.spec_from_file_location("rust_ci", Path("__RUN_RUST_CI__"))
ci = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci)


def checked(*args, **kwargs):
    if args[0] == "docker":
        return json.dumps([{"Mounts": [{"Source": os.environ["STUB_WORK"],
                                        "Destination": "/work"}]}])
    if args[0] == "git" and "rev-parse" in args:
        return "0" * 40
    if args[0] == "git":
        return ""
    raise AssertionError(f"unexpected checked call: {args}")


def wait_for(predicate, seconds=90):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("bounded wait expired")


if __name__ == "__main__":
    work = Path(os.environ["STUB_WORK"])
    if os.environ.get("STUB_MODE") == "cohort_refuse":
        print("rust_ci_control=refused reason=capacity_slots_timeout", flush=True)
        sys.exit(75)
    print(json.dumps({"rust_ci": "waiting_for_slots", "count": 2,
                      "timeout_seconds": 600}), flush=True)
    while not (work / "release").exists():
        time.sleep(0.05)
    time.sleep(0.5)
    print("rust_ci_control=pass case=concurrent_cache_and_cancel_isolation "
          "spare_slot_preserved=true", flush=True)
    shutil.rmtree(work.parent)
    sys.exit(0)
'''

CONTROLLER_STUB = '''\
"""Hermetic stub of the run-rust-ci.py CLI: one sub-run role per invocation."""
import json
import os
from pathlib import Path
import sys
import time

args = sys.argv[1:]
script = args[args.index("--script") + 1]
work = Path(os.environ["STUB_WORK"])
mode = os.environ.get("STUB_MODE", "")

if script == "normal.sh":
    if mode == "normal_refuse":
        print(json.dumps({"rust_ci": "refused", "reason": "capacity_slots_timeout",
                          "active": 2, "available_memory": 1234567}), flush=True)
        sys.exit(75)
    work.mkdir(parents=True, exist_ok=True)
    (work / "normal-cache").write_text("keep")
    print(json.dumps({"rust_ci": "starting", "name": "pf-rust-stub-normal"}), flush=True)
    (work / "ready").touch()
    while not (work / "release").exists():
        time.sleep(0.05)
    print("normal_sibling=pass", flush=True)
    sys.exit(0)

if script == "quick.sh":
    if mode == "quick_refuse":
        print(json.dumps({"rust_ci": "refused", "reason": "capacity_slots_timeout",
                          "active": 2, "available_memory": 1234567}), flush=True)
        sys.exit(75)
    if mode == "quick_identity_refuse":
        print(json.dumps({"rust_ci": "refused", "reason": "source_identity",
                          "active": 0, "available_memory": 1234567}), flush=True)
        sys.exit(75)
    if mode == "quick_empty":
        sys.exit(0)
    if mode == "quick_fail":
        sys.exit(3)
    print("spare_slot_normal=pass", flush=True)
    sys.exit(0)

sys.exit(2)
'''


class SlotContentionSubrunRefusalTests(unittest.TestCase):
    """The real control process against stub sub-runs: no docker/git/network.

    A sub-run's typed environmental capacity refusal (rc 75 plus its typed
    line) must surface as the control's own typed refusal (rc 75), never as
    an rc-1 failure of unrelated PRs and never as a pass of the property.
    Any other sub-run outcome (rc 0 without the marker, a non-75 failure, or
    rc 75 with a non-capacity reason) is a real semantics break: rc 1.
    """

    REFUSED_LINE = "rust_ci_control=refused reason=capacity_slots_timeout"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="pf-slot-contention-refusal-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        control_dir = self.root / "control"
        control_dir.mkdir()
        tests = Path(__file__).parent
        self.control = control_dir / "slot_contention.py"
        self.control.write_bytes((tests / "slot_contention.py").read_bytes())
        (control_dir / "live_rust_ci.py").write_text(
            LIVE_STUB.replace("__RUN_RUST_CI__", str(tests.parent / "scripts/run-rust-ci.py")))
        (control_dir / "controller.py").write_text(CONTROLLER_STUB)
        self.work = self.root / "runs" / "work"

    def run_control(self, mode):
        env = dict(os.environ, STUB_WORK=str(self.work), STUB_MODE=mode)
        return subprocess.run([sys.executable, str(self.control), "--image", CI_RUST_IMAGE],
                              capture_output=True, text=True, timeout=240,
                              env=env, cwd=str(self.control.parent))

    def failure(self, result):
        return f"rc={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    def test_healthy_subruns_pass_the_control(self):
        result = self.run_control("")
        self.assertEqual(result.returncode, 0, self.failure(result))
        self.assertIn("rust_ci_control=pass case=ordinary_sibling_then_atomic_cohort",
                      result.stdout)

    def test_quick_capacity_refusal_exits_75_typed_not_1(self):
        result = self.run_control("quick_refuse")
        self.assertEqual(result.returncode, 75, self.failure(result))
        self.assertIn(self.REFUSED_LINE, result.stdout)
        self.assertNotIn("rust_ci_control=pass", result.stdout)
        self.assertNotIn("spare_slot_normal=pass", result.stdout)

    def test_normal_startup_capacity_refusal_exits_75_typed(self):
        result = self.run_control("normal_refuse")
        self.assertEqual(result.returncode, 75, self.failure(result))
        self.assertIn(self.REFUSED_LINE, result.stdout)
        self.assertNotIn("rust_ci_control=pass", result.stdout)

    def test_cohort_capacity_refusal_exits_75_typed(self):
        result = self.run_control("cohort_refuse")
        self.assertEqual(result.returncode, 75, self.failure(result))
        self.assertIn(self.REFUSED_LINE, result.stdout)
        self.assertNotIn("rust_ci_control=pass", result.stdout)

    def test_quick_zero_without_marker_still_fails(self):
        result = self.run_control("quick_empty")
        self.assertEqual(result.returncode, 1, self.failure(result))
        self.assertNotIn("rust_ci_control=refused", result.stdout)
        self.assertNotIn("rust_ci_control=pass", result.stdout)

    def test_quick_non75_failure_still_fails(self):
        result = self.run_control("quick_fail")
        self.assertEqual(result.returncode, 1, self.failure(result))
        self.assertNotIn("rust_ci_control=refused", result.stdout)
        self.assertNotIn("rust_ci_control=pass", result.stdout)

    def test_quick_rc75_with_non_capacity_reason_still_fails(self):
        result = self.run_control("quick_identity_refuse")
        self.assertEqual(result.returncode, 1, self.failure(result))
        self.assertNotIn("rust_ci_control=refused", result.stdout)
        self.assertNotIn("rust_ci_control=pass", result.stdout)


class ControlsStepTypedSkipTests(unittest.TestCase):
    """The workflow maps the control's typed refusal to a visible skip.

    Runs the committed `id: controls` step script under bash with a fake
    python3 on PATH: rc 75 plus the typed refused line is a green, loud skip
    (output, step summary, warning annotation, never a pass of the property);
    rc 75 without the typed line and every other rc stay red.
    """

    WORKFLOW = Path(__file__).parents[1] / ".github/workflows/rust-container.yml"

    def step_script(self):
        lines = self.WORKFLOW.read_text().splitlines()
        start = lines.index("        id: controls")
        run = lines.index("        run: |", start)
        script = []
        for line in lines[run + 1:]:
            if line.strip() and not line.startswith("          "):
                break
            script.append(line[10:] if line.startswith("          ") else "")
        return "\n".join(script) + "\n"

    def run_step(self, root, rc, out):
        shim = root / "bin"
        shim.mkdir()
        (shim / "python3").write_text('#!/bin/sh\necho "$FAKE_OUT"\nexit "$FAKE_RC"\n')
        (shim / "python3").chmod(0o755)
        script = root / "step.sh"
        script.write_text(self.step_script())
        env = dict(os.environ, PATH=f"{shim}:{os.environ['PATH']}", FAKE_RC=str(rc),
                   FAKE_OUT=out, RUNNER_TEMP=str(root), GITHUB_OUTPUT=str(root / "output"),
                   GITHUB_STEP_SUMMARY=str(root / "summary"))
        return subprocess.run(["bash", "-e", str(script)], capture_output=True,
                              text=True, timeout=60, env=env)

    def read(self, root, name):
        path = root / name
        return path.read_text() if path.exists() else ""

    def test_typed_refusal_is_a_loud_green_skip(self):
        with tempfile.TemporaryDirectory(prefix="pf-controls-step-") as tmp:
            root = Path(tmp)
            result = self.run_step(root, 75, "rust_ci_control=refused reason=capacity_slots_timeout")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(self.read(root, "output"), "controls-refusal=capacity_slots_timeout\n")
            summary = self.read(root, "summary")
            self.assertIn("capacity_slots_timeout", summary)
            self.assertIn("NOT passed", summary)
            self.assertIn("::warning::rust_ci_control=refused reason=capacity_slots_timeout", result.stdout)

    def test_rc75_without_typed_line_stays_red(self):
        with tempfile.TemporaryDirectory(prefix="pf-controls-step-") as tmp:
            root = Path(tmp)
            result = self.run_step(root, 75, "segfault or any other untyped exit")
            self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
            self.assertEqual(self.read(root, "output"), "")

    def test_real_control_failure_stays_red(self):
        with tempfile.TemporaryDirectory(prefix="pf-controls-step-") as tmp:
            root = Path(tmp)
            result = self.run_step(root, 1, "AssertionError: spare slot semantics broke")
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertEqual(self.read(root, "output"), "")

    def test_pass_run_keeps_no_refusal_output(self):
        with tempfile.TemporaryDirectory(prefix="pf-controls-step-") as tmp:
            root = Path(tmp)
            result = self.run_step(root, 0, "rust_ci_control=pass case=ordinary_sibling_then_atomic_cohort")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(self.read(root, "output"), "")

    def test_refusal_output_is_exposed_to_callers_and_timeout_unchanged(self):
        text = self.WORKFLOW.read_text()
        self.assertIn("controls-refusal: ${{ steps.controls.outputs.controls-refusal }}", text)
        self.assertIn("value: ${{ jobs.container.outputs.controls-refusal }}", text)
        self.assertEqual(text.count("timeout-minutes: 30"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
