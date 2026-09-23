#!/usr/bin/env python3
"""Host engine/admission boundary. Task tools run only in the pinned image.

Two concurrent slots, conservative per-run reservations and private bounded
sccache. No persistent target/registry cache or cache export. Failed engine
cleanup retains the reservation until a later admission proves its owner gone;
only the exact container recorded by that reservation is removed.
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
import time

GIB = 1024**3
DISK = 8 * GIB
MEMORY = 4 * GIB
INODES = 200000
IMAGE = re.compile(r"10\.0\.32\.86:5555/pocketforge/ci-rust@sha256:[a-f0-9]{64}")
SHA = re.compile(r"[a-f0-9]{40}")
SLOT_WAIT_SECONDS = 600


class Refused(Exception):
    pass


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


def active_reservations(root):
    """Count live reservations and reclaim only provably dead owners."""
    active = 0
    for reservation_path in root.glob("run-*/reservation.json"):
        directory = reservation_path.parent
        lock_path = directory / "run.lock"
        if not lock_path.exists():
            print(json.dumps({"rust_ci": "reservation_alive", "reason": "missing_run_lock",
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
                shutil.rmtree(directory)
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


def run(source, source_sha, platform, platform_sha, image, script, *, root=None, slot_fd=None):
    if not IMAGE.fullmatch(image):
        raise Refused("unpinned_image")
    if not re.fullmatch(r"[A-Za-z0-9_./-]+\.sh", script) or ".." in script or script.startswith("/"):
        raise Refused("invalid_script")
    verify_source(source, source_sha)
    verify_source(platform, platform_sha)
    # Archive only committed files: no checkout tokens, .git, host homes or tools.
    root = Path(root or state_root())
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = None
    name = None
    cleaned = True
    slot = None
    run_guard = None
    child = None
    try:
        slot = adopt_slot(root, slot_fd) if slot_fd is not None else acquire_slots(root)[0]
        with admission():
            active = active_reservations(root)
            docker_root = output("docker", "info", "--format", "{{.DockerRootDir}}")
            memory = next(int(line.split()[1]) * 1024 for line in Path("/proc/meminfo").read_text().splitlines()
                          if line.startswith("MemAvailable:"))
            capacity([root, docker_root], active, memory)
            directory = Path(tempfile.mkdtemp(prefix="run-", dir=root))
            run_guard = (directory / "run.lock").open("a")
            fcntl.flock(run_guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            name = f"pf-rust-{os.getuid()}-{directory.name}"
            reservation = dict(name=name, disk=DISK, memory=MEMORY, inodes=INODES,
                               image=image, source_sha=source_sha, platform_sha=platform_sha,
                               run_id=os.environ.get("GITHUB_RUN_ID", "local"),
                               run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", "0"))
            with (directory / "reservation.json").open("w") as stream:
                json.dump(reservation, stream)
                stream.flush()
                os.fsync(stream.fileno())
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
            "--env", "PF_PLATFORM_DIR=/work/platform", image, "bash", "-euc",
            'mkdir -p /work/source /work/platform; tar -xf /input/source.tar -C /work/source; '
            'tar -xf /input/platform.tar -C /work/platform; cd /work/source; exec bash "$1"',
            "rust-ci", script,
        ]
        cleaned = False  # Even a failed create can have allocated an engine object.
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
        print(json.dumps({"rust_ci": "starting", **reservation}), flush=True)
        child = subprocess.Popen(["docker", "start", "--attach", name])
        while child.poll() is None:
            # Run-private output budget; no unbounded target/cache exports. Admission
            # is conservative accounting, not an underlying filesystem hard quota.
            used = disk_usage(directory)
            if used > DISK:
                raise Refused("run_disk_budget")
            time.sleep(2)
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
            if directory and cleaned:
                shutil.rmtree(directory)
            elif directory:
                print(json.dumps({"rust_ci": "retained", "reason": "engine_cleanup_unconfirmed",
                                  "directory": str(directory)}), flush=True)
            if child is not None and cleaned:
                child.wait(timeout=10)
        finally:
            if slot:
                slot.close()
            if run_guard:
                run_guard.close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)


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
        print(json.dumps({"rust_ci": "refused", "reason": str(exc)}), flush=True)
        return 75


if __name__ == "__main__":
    sys.exit(main())
