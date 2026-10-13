#!/usr/bin/env python3
"""Host engine/admission boundary. Task tools run only in the pinned image.

Two concurrent slots, conservative per-run reservations and private bounded
sccache. No persistent target/registry cache or cache export. Failed engine
cleanup retains the reservation until a later admission proves its owner gone;
only the exact container recorded by that reservation is removed. A capacity
refusal while sibling reservations are active is contention, not a verdict: it
waits (bounded, without holding a slot) instead of refusing.
"""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

GIB = 1024**3
DISK = 8 * GIB
MEMORY = 4 * GIB
INODES = 200000
CI_RUST_IMAGE = "10.0.32.86:5555/pocketforge/ci-rust@sha256:801f15f7e8fd20b449e3b0d0a7a56d632e28dae825b251a19a30e6f8b32bd8e9"
CI_TOOLS_IMAGE = "10.0.32.86:5555/pocketforge/ci-tools@sha256:2ff536627228c90e5c32b7d6345b21600a38a17481c075b41683db47ead3e862"
ALLOWED_IMAGES = frozenset((CI_RUST_IMAGE, CI_TOOLS_IMAGE))
SHA = re.compile(r"[a-f0-9]{40}")
SLOT_WAIT_SECONDS = 600
CAPACITY_RECHECK_SECONDS = 2
CAPACITY_REASONS = ("disk_capacity", "inode_capacity", "memory_capacity")


class Refused(Exception):
    pass


def verify_image(image):
    """Admit only reviewed repository+digest pairs, never arbitrary digests."""
    if image not in ALLOWED_IMAGES:
        raise Refused("unpinned_image")


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def capacity(paths, active, available_memory):
    """Count outstanding reservations even when partially written (conservative)."""
    for path in paths:
        fs = os.statvfs(path)
        if fs.f_bavail * fs.f_frsize < (active + 1) * DISK + 9 * GIB:
            raise Refused("disk_capacity")
        if fs.f_favail < (active + 1) * INODES + 100000:
            raise Refused("inode_capacity")
    if available_memory < (active + 1) * MEMORY + 2 * GIB:
        raise Refused("memory_capacity")


@contextlib.contextmanager
def admission():
    # Share the existing shell CI pilot's short admission lock. Rust additionally
    # retains a durable reservation for its entire lifetime, including setup.
    root = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / f"pf-ci-admission-{os.getuid()}"
    root.mkdir(mode=0o700, exist_ok=True)
    with (root / "lock").open("a") as guard:
        until = time.monotonic() + 30
        while True:
            try:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= until:
                    raise Refused("admission_busy") from None
                time.sleep(0.2)
        yield


def state_root():
    return Path(f"/tmp/pf-rust-ci-{os.getuid()}")


def fsync_directory(path):
    """Persist directory-entry ordering used by the reservation protocol."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_reservation(root, reservation):
    """Lock a hidden staging directory, then publish one complete run atomically."""
    staging = Path(tempfile.mkdtemp(prefix=".run-", dir=root))
    published = root / staging.name.removeprefix(".")
    guard = None
    try:
        guard = (staging / "run.lock").open("a")
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        reservation = dict(reservation,
                           name=f"pf-rust-{os.getuid()}-{published.name}")
        temporary = staging / ".reservation.json.tmp"
        with temporary.open("w") as stream:
            json.dump(reservation, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, staging / "reservation.json")
        fsync_directory(staging)
        staging.rename(published)
        fsync_directory(root)
        return published, guard, reservation
    except BaseException:
        if guard is not None:
            guard.close()
        candidate = published if published.exists() else staging
        if candidate.exists():
            shutil.rmtree(candidate)
        raise


def cleanup_reservation(directory, guard):
    """Unpublish identity durably before releasing and removing its lock."""
    (directory / "reservation.json").unlink(missing_ok=True)
    fsync_directory(directory)
    if guard is not None:
        guard.close()
    (directory / "run.lock").unlink(missing_ok=True)
    shutil.rmtree(directory)
    fsync_directory(directory.parent)


def refusal_event(exc, root=None):
    """Give every refusal enough capacity context to diagnose contention."""
    root = Path(root or state_root())
    try:
        active = sum(1 for _ in root.glob("run-*/reservation.json"))
    except OSError:
        active = None
    try:
        available_memory = host_memory()
    except (OSError, StopIteration, ValueError):
        available_memory = None
    return {"rust_ci": "refused", "reason": str(exc), "active": active,
            "available_memory": available_memory}


def remove_reserved_container(name):
    """Remove one exact container, or positively establish that it is absent."""
    result = subprocess.run(["docker", "rm", "-f", name], timeout=20,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode == 0:
        return True
    inspected = subprocess.run(["docker", "container", "inspect", name], timeout=20,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    missing = f"Error: No such container: {name}"
    return inspected.returncode == 1 and missing in inspected.stderr.splitlines()


def inspect_reserved_container(name):
    """Return present/absent only when the engine proves the exact identity."""
    inspected = subprocess.run(["docker", "container", "inspect", name], timeout=20,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if inspected.returncode == 0:
        return True
    missing = f"Error: No such container: {name}"
    if inspected.returncode == 1 and missing in inspected.stderr.splitlines():
        return False
    raise OSError("container identity unconfirmed")


def active_reservations(root):
    """Count live reservations and reclaim only provably dead owners."""
    active = 0
    for reservation_path in root.glob("run-*/reservation.json"):
        directory = reservation_path.parent
        lock_path = directory / "run.lock"
        if not lock_path.exists():
            # Releases before 3200063 created reservation.json without a lifetime
            # lock. Upgrade leftovers are safe to remove only when the engine
            # proves their exact recorded container is absent. A present or
            # unconfirmable container remains reserved; absence is never inferred.
            try:
                reservation = json.loads(reservation_path.read_text())
                name = reservation["name"]
                if not isinstance(name, str) or not name:
                    raise ValueError("invalid container name")
                present = inspect_reserved_container(name)
                if present:
                    print(json.dumps({"rust_ci": "reservation_alive",
                                      "reason": "missing_run_lock", "name": name,
                                      "directory": str(directory)}), flush=True)
                    active += 1
                else:
                    reservation_path.unlink()
                    fsync_directory(directory)
                    shutil.rmtree(directory)
                    fsync_directory(root)
                    print(json.dumps({"rust_ci": "reclaimed",
                                      "reason": "legacy_owner_gone", "name": name,
                                      "directory": str(directory)}), flush=True)
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError,
                    subprocess.TimeoutExpired):
                print(json.dumps({"rust_ci": "reservation_alive",
                                  "reason": "legacy_owner_unconfirmed",
                                  "directory": str(directory)}), flush=True)
                active += 1
            continue
        guard = lock_path.open("a")
        try:
            try:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                active += 1
                continue
            try:
                reservation = json.loads(reservation_path.read_text())
                name = reservation["name"]
                if not isinstance(name, str) or not name:
                    raise ValueError("invalid container name")
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                print(json.dumps({"rust_ci": "reservation_alive",
                                  "reason": "reservation_identity_unknown",
                                  "directory": str(directory)}), flush=True)
                active += 1
                continue
            if remove_reserved_container(name):
                cleanup_reservation(directory, guard)
                guard = None
                print(json.dumps({"rust_ci": "reclaimed", "reason": "owner_gone",
                                  "name": name, "directory": str(directory)}), flush=True)
            else:
                print(json.dumps({"rust_ci": "reservation_alive",
                                  "reason": "engine_cleanup_unconfirmed",
                                  "name": name, "directory": str(directory)}), flush=True)
                active += 1
        except (OSError, subprocess.TimeoutExpired):
            print(json.dumps({"rust_ci": "reservation_alive",
                              "reason": "reclaim_unconfirmed",
                              "directory": str(directory)}), flush=True)
            active += 1
        finally:
            if guard is not None:
                guard.close()
    return active


def acquire_slots(root, count=1, *, wait_seconds=SLOT_WAIT_SECONDS):
    """Atomically claim a single run or the two-control cohort. No hold-and-wait.

    Failed attempts release EVERY partial slot and the admission lock before
    waiting, so existing siblings finish and ordinary jobs can use spare slots.
    """
    if count not in (1, 2) or not 0 <= wait_seconds <= SLOT_WAIT_SECONDS:
        raise Refused("invalid_slot_request")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_seconds
    announced = False
    while True:
        acquired = []
        complete = False
        try:
            with admission():
                for number in range(2):
                    guard = (root / f"slot-{number}.lock").open("a")
                    try:
                        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired.append(guard)
                    except BlockingIOError:
                        guard.close()
                    if len(acquired) == count:
                        complete = True
                        return acquired
        finally:
            if not complete:
                for guard in acquired:
                    guard.close()
        if time.monotonic() >= deadline:
            raise Refused("capacity_slots_timeout")
        if not announced:
            print(json.dumps({"rust_ci": "waiting_for_slots", "count": count,
                              "timeout_seconds": wait_seconds}), flush=True)
            announced = True
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))


def adopt_slot(root, fd):
    """A cohort child inherits one real lifetime lock, never a bypass flag."""
    identity = os.fstat(fd)
    allowed = []
    for number in range(2):
        path = root / f"slot-{number}.lock"
        if path.exists():
            st = path.stat()
            allowed.append((st.st_dev, st.st_ino))
    if (identity.st_dev, identity.st_ino) not in allowed:
        raise Refused("invalid_inherited_slot")
    guard = os.fdopen(os.dup(fd), "a")
    try:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        guard.close()
        raise
    return guard


def host_memory():
    """MemAvailable already excludes resident sibling usage; never add it back."""
    return next(int(line.split()[1]) * 1024
                for line in Path("/proc/meminfo").read_text().splitlines()
                if line.startswith("MemAvailable:"))


def admit(root, reserve, *, slot=None, wait_seconds=SLOT_WAIT_SECONDS):
    """Run reserve(active) under the admission lock, waiting out live siblings.

    A memory, disk or inode refusal while other reservations are active is
    contention, not a verdict: release the pool slot and the admission lock
    before waiting (no hold-and-wait), announce waiting_for_capacity once,
    sleep briefly and re-check within the wait_seconds deadline. Refuse with
    the original reason when nothing else is active or the deadline passes.
    An inherited cohort slot belongs to the parent and cannot be released,
    so such a run never waits. Returns (slot, reserve's result).
    """
    if not 0 <= wait_seconds <= SLOT_WAIT_SECONDS:
        raise Refused("invalid_capacity_wait")
    inherited = slot is not None
    deadline = time.monotonic() + wait_seconds
    announced = False
    while True:
        refusal = None
        admitted = False
        result = None
        if not inherited:
            slot = acquire_slots(root, wait_seconds=max(0, deadline - time.monotonic()))[0]
        try:
            with admission():
                active = active_reservations(root)
                try:
                    result = reserve(active)
                except Refused as exc:
                    refusal = exc
                    if (inherited or str(exc) not in CAPACITY_REASONS or active == 0
                            or time.monotonic() >= deadline):
                        raise
                else:
                    admitted = True
        finally:
            if not admitted and not inherited and slot is not None:
                slot.close()
                slot = None
        if admitted:
            return slot, result
        if not announced:
            print(json.dumps({"rust_ci": "waiting_for_capacity", "reason": str(refusal),
                              "timeout_seconds": wait_seconds}), flush=True)
            announced = True
        time.sleep(min(CAPACITY_RECHECK_SECONDS, max(0, deadline - time.monotonic())))


def verify_source(path, sha):
    if not SHA.fullmatch(sha) or output("git", "-C", str(path), "rev-parse", "HEAD") != sha:
        raise Refused("source_identity")
    if output("git", "-C", str(path), "status", "--porcelain", "--untracked-files=no"):
        raise Refused("dirty_source")


def archive(path, sha, destination):
    with destination.open("wb") as stream:
        subprocess.run(["git", "-C", str(path), "archive", sha], stdout=stream, check=True)


def disk_usage(root):
    """Allocated bytes during compilation; vanished intermediates are normal.

    GNU du exits nonzero when rustc unlinks an object between readdir/stat. Do
    not abort a healthy job for that race; other I/O errors still fail closed.
    """
    pending = [root]
    seen = set()
    total = 0
    while pending:
        path = pending.pop()
        try:
            info = path.lstat()
            identity = (info.st_dev, info.st_ino)
            if identity in seen:
                continue
            seen.add(identity)
            total += info.st_blocks * 512
            if stat.S_ISDIR(info.st_mode):
                with os.scandir(path) as entries:
                    pending.extend(Path(entry.path) for entry in entries)
        except FileNotFoundError:
            if path == root:
                raise Refused("run_disk_usage_unknown") from None
        except OSError:
            raise Refused("run_disk_usage_unknown") from None
    return total


class DiskAccounting:
    """Thread-safe sampled peaks for run-private allocated bytes."""

    def __init__(self, budget):
        self.budget = budget
        self.peak = 0
        self.phase = "preflight"
        self.phases = {self.phase: 0}
        self.lock = threading.Lock()

    def observe(self, used):
        with self.lock:
            self.peak = max(self.peak, used)
            if self.phase is not None:
                self.phases[self.phase] = max(self.phases.get(self.phase, 0), used)

    def enforce(self, used):
        self.observe(used)
        if used > self.budget:
            raise Refused("run_disk_budget")

    def start_phase(self, name, used):
        with self.lock:
            self.peak = max(self.peak, used)
            self.phase = name
            self.phases[name] = max(self.phases.get(name, 0), used)

    def finish_phase(self, used):
        self.observe(used)
        with self.lock:
            self.phase = None

    def event(self):
        with self.lock:
            return {
                "rust_ci": "disk_accounting",
                "budget_bytes": self.budget,
                "peak_private_bytes": self.peak,
                "phases": [{"phase": phase, "peak_private_bytes": peak}
                           for phase, peak in self.phases.items()],
            }


def relay_container_output(stream, directory, accounting, errors):
    """Preserve workload output while assigning boundary samples to its groups."""
    try:
        for line in stream:
            print(line, end="", flush=True)
            if line.startswith("::group::"):
                used = disk_usage(directory)
                accounting.enforce(used)
                accounting.start_phase(line.removeprefix("::group::").strip(), used)
            elif line.strip() == "::endgroup::":
                used = disk_usage(directory)
                accounting.enforce(used)
                accounting.finish_phase(used)
    except BaseException as exc:
        errors.append(exc)


def run(source, source_sha, platform, platform_sha, image, script, *, root=None, slot_fd=None,
        disk_budget=DISK):
    accounting = DiskAccounting(disk_budget)
    directory = None
    name = None
    cleaned = True
    slot = None
    run_guard = None
    child = None
    reader = None
    reader_errors = []

    def reserve(active):
        """Capacity verdict and durable reservation, atomic under the admission lock."""
        nonlocal directory, run_guard, name
        docker_root = output("docker", "info", "--format", "{{.DockerRootDir}}")
        capacity([root, docker_root], active, host_memory())
        reservation = dict(disk=DISK, memory=MEMORY, inodes=INODES, image=image,
                           source_sha=source_sha, platform_sha=platform_sha,
                           run_id=os.environ.get("GITHUB_RUN_ID", "local"),
                           run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", "0"))
        directory, run_guard, reservation = publish_reservation(root, reservation)
        name = reservation["name"]
        return reservation

    try:
        verify_image(image)
        if (not re.fullmatch(r"[A-Za-z0-9_./-]+\.sh", script) or ".." in script
                or script.startswith("/")):
            raise Refused("invalid_script")
        verify_source(source, source_sha)
        verify_source(platform, platform_sha)
        # Archive only committed files: no checkout tokens, .git, host homes or tools.
        root = Path(root or state_root())
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if slot_fd is not None:
            slot = adopt_slot(root, slot_fd)
        slot, reservation = admit(root, reserve, slot=slot)
        # The slot + durable reservation remain held while pulling/staging/running.
        subprocess.run(["docker", "pull", "--quiet", image], check=True, stdout=subprocess.DEVNULL)
        source_input = directory / "input"
        source_input.mkdir()
        work = directory / "work"
        work.mkdir()
        archive(source, source_sha, source_input / "source.tar")
        archive(platform, platform_sha, source_input / "platform.tar")
        epoch = output("git", "-C", str(source), "show", "-s", "--format=%ct", source_sha)
        command = [
            "docker", "create", "--init", "--name", name,
            "--label", "pocketforge.rust-ci=true", "--label", f"pocketforge.source-sha={source_sha}",
            "--user", f"{os.getuid()}:{os.getgid()}", "--cpus", "2", "--memory", str(MEMORY),
            "--memory-swap", str(MEMORY), "--pids-limit", "512", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--read-only",
            "--log-opt", "max-size=10m", "--log-opt", "max-file=1",
            "--tmpfs", "/tmp:rw,nosuid,nodev,exec,size=256m,mode=1777",
            "--mount", f"type=bind,src={source_input},dst=/input,readonly",
            "--mount", f"type=bind,src={work},dst=/work",
            "--env", f"PF_SOURCE_SHA={source_sha}", "--env", f"PF_PLATFORM_SHA={platform_sha}",
            "--env", f"PF_IMAGE={image}", "--env", f"SOURCE_DATE_EPOCH={epoch}",
            "--env", "PF_PLATFORM_DIR=/work/platform",
            # Every run starts with an empty private target. Incremental state and
            # debug sections only consume the guarded producer budget; none of the
            # pass/fail phases consume them.
            "--env", "CARGO_INCREMENTAL=0",
            "--env", "CARGO_PROFILE_DEV_DEBUG=0",
            "--env", "CARGO_PROFILE_TEST_DEBUG=0",
            image, "bash", "-euc",
            'mkdir -p /work/source /work/platform; tar -xf /input/source.tar -C /work/source; '
            'tar -xf /input/platform.tar -C /work/platform; cd /work/source; exec bash "$1"',
            "rust-ci", script,
        ]
        cleaned = False  # Even a failed create can have allocated an engine object.
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
        print(json.dumps({"rust_ci": "starting", **reservation}), flush=True)
        child = subprocess.Popen(["docker", "start", "--attach", name], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
        reader = threading.Thread(target=relay_container_output,
                                  args=(child.stdout, directory, accounting, reader_errors),
                                  daemon=True)
        reader.start()
        while child.poll() is None:
            # Run-private output budget; no unbounded target/cache exports. Admission
            # is conservative accounting, not an underlying filesystem hard quota.
            used = disk_usage(directory)
            accounting.enforce(used)
            time.sleep(2)
        reader.join(timeout=10)
        if reader.is_alive():
            raise Refused("run_output_stream_stalled")
        if reader_errors:
            raise reader_errors[0]
        accounting.enforce(disk_usage(directory))
        rc = child.returncode
        print(json.dumps({"rust_ci": "finished", "name": name, "exit_code": rc}), flush=True)
        return rc
    finally:
        # Ignore repeated signals only during bounded cleanup. Never enumerate and
        # delete by shared labels or run ID; the random name belongs to this call.
        previous = {s: signal.signal(s, signal.SIG_IGN) for s in (signal.SIGINT, signal.SIGTERM)}
        try:
            if name and not cleaned:
                try:
                    result = subprocess.run(["docker", "rm", "-f", name], timeout=20,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    cleaned = result.returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    cleaned = False
            if child is not None and cleaned:
                child.wait(timeout=10)
                if reader is not None:
                    reader.join(timeout=10)
            if directory and directory.exists():
                try:
                    accounting.observe(disk_usage(directory))
                except Refused:
                    pass
            if directory and cleaned:
                cleanup_reservation(directory, run_guard)
                run_guard = None
            elif directory:
                print(json.dumps({"rust_ci": "retained", "reason": "engine_cleanup_unconfirmed",
                                  "directory": str(directory)}), flush=True)
        finally:
            if slot:
                slot.close()
            if run_guard:
                run_guard.close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            print(json.dumps(accounting.event()), flush=True)


def interrupted(signum, _frame):
    raise SystemExit(128 + signum)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "source-sha", "platform", "platform-sha", "image", "script"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--slot-fd", type=int, help="internal: inherited lifetime slot from an admitted control cohort")
    a = p.parse_args()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, interrupted)
    try:
        return run(Path(a.source), a.source_sha, Path(a.platform), a.platform_sha, a.image, a.script,
                   slot_fd=a.slot_fd)
    except Refused as exc:
        print(json.dumps(refusal_event(exc)), flush=True)
        return 75


if __name__ == "__main__":
    sys.exit(main())
