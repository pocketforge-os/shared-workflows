#!/usr/bin/env python3
"""Real engine controls: missing tool, concurrent cache writers, cancellation.

Runs through the production controller with tiny committed fixture repositories.
Never changes runner tools or published image bytes. Needs only host stdlib/Git/
Docker, just like the controller. Logs and reservations use run-private names.
"""
import argparse
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

CONTROLLER = Path(__file__).parents[1] / "scripts/run-rust-ci.py"
spec = importlib.util.spec_from_file_location("rust_ci", CONTROLLER)
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


def checked(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def wait_for(predicate, seconds=90):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.25)
    raise AssertionError("bounded wait expired")


def controls(args, slots):
    checked("docker", "pull", "--quiet", args.image)
    # Removing the real cargo binary in an ephemeral writable container must
    # fail at preflight, before a payload can falsely pass via a host fallback.
    with tempfile.TemporaryDirectory(prefix="pf-rust-controls-") as tmp:
        root = Path(tmp)
        broken = "pf-rust-missing-" + root.name
        try:
            missing = subprocess.run([
                "docker", "run", "--rm", "--name", broken, "--user", "0:0",
                "--cpus", "1", "--memory", "256m", "--pids-limit", "64",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--entrypoint", "/bin/bash", args.image, "-c",
                "rm /opt/rust/bin/cargo; exec /usr/local/bin/pf-rust-entrypoint true"],
                text=True, capture_output=True, timeout=30)
            assert missing.returncode == 2, missing
            assert "reason=missing_tool tool=cargo" in missing.stderr, missing.stderr
        finally:
            subprocess.run(["docker", "rm", "-f", broken], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("rust_ci_control=pass case=real_missing_cargo", flush=True)
        source = root / "source"
        source.mkdir()
        (source / "ci.sh").write_text('''#!/bin/bash
set -euo pipefail
test ! -e /var/run/docker.sock
test ! -e /root/.cargo
if touch /input/forbidden 2>/dev/null; then exit 42; fi
printf 'pub fn answer() -> u32 { 42 }\n' > /work/cache-probe.rs
sccache rustc --crate-name same_key --crate-type rlib --emit=link /work/cache-probe.rs --out-dir /work
rm /work/libsame_key.rlib
sccache rustc --crate-name same_key --crate-type rlib --emit=link /work/cache-probe.rs --out-dir /work
sccache --show-stats | tee /work/cache-stats
grep -Eq '^Cache hits[[:space:]]+[1-9][0-9]*$' /work/cache-stats
printf ready > /work/ready
while test ! -e /work/release; do sleep 0.2; done
test -s /work/libsame_key.rlib
test -n "$(find "$SCCACHE_DIR" -type f -print -quit)"
printf 'sibling cache preserved\n'
''')
        checked("git", "init", "-q", str(source))
        checked("git", "-C", str(source), "add", "ci.sh")
        checked("git", "-C", str(source), "-c", "user.name=CI fixture", "-c",
                "user.email=ci@example.invalid", "commit", "-qm", "isolated cache fixture")
        sha = checked("git", "-C", str(source), "rev-parse", "HEAD")
        command = [sys.executable, str(CONTROLLER), "--source", str(source), "--source-sha", sha,
                   "--platform", str(source), "--platform-sha", sha, "--image", args.image, "--script", "ci.sh"]
        children = []
        logs = []
        names = []
        directories = []
        try:
            for number in range(2):
                log = root / f"{number}.log"
                logs.append(log)
                with log.open("w") as stream:
                    fd = slots[number].fileno()
                    children.append(subprocess.Popen(command + ["--slot-fd", str(fd)], pass_fds=(fd,),
                                                     stdout=stream, stderr=subprocess.STDOUT))
                def identity():
                    assert children[-1].poll() is None, log.read_text()
                    for line in log.read_text().splitlines():
                        if line.startswith('{"rust_ci": "starting"'):
                            return json.loads(line)["name"]
                name = wait_for(identity)
                names.append(name)
                details = json.loads(checked("docker", "inspect", name))[0]
                assert details["HostConfig"]["NanoCpus"] == 2000000000
                assert details["HostConfig"]["Memory"] == 4 * 1024**3
                work = Path(next(m["Source"] for m in details["Mounts"] if m["Destination"] == "/work"))
                directories.append(work.parent)
                wait_for(lambda: (work / "ready").exists())
            assert directories[0] != directories[1]
            # Both compiled the exact same sccache key twice, with independent
            # daemons/cache paths. A SIGTERM must reap only the first run.
            children[0].send_signal(signal.SIGTERM)
            assert children[0].wait(timeout=30) == 143, logs[0].read_text()
            assert not directories[0].exists()
            assert subprocess.run(["docker", "inspect", names[0]], capture_output=True).returncode != 0
            assert children[1].poll() is None
            assert (directories[1] / "work/ready").exists()
            (directories[1] / "work/release").touch()
            assert children[1].wait(timeout=30) == 0, logs[1].read_text()
            assert "sibling cache preserved" in logs[1].read_text()
            assert not directories[1].exists()
            assert subprocess.run(["docker", "inspect", names[1]], capture_output=True).returncode != 0
            print("rust_ci_control=pass case=concurrent_cache_and_cancel_isolation", flush=True)
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=30)
            for log in logs:
                print(log.read_text())
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--image", required=True)
    p.add_argument("--wait-seconds", type=float, default=ci.SLOT_WAIT_SECONDS)
    args = p.parse_args()
    assert ci.IMAGE.fullmatch(args.image)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, ci.interrupted)
    try:
        with contextlib.ExitStack() as cleanup:
            slots = ci.acquire_slots(ci.state_root(), 2, wait_seconds=args.wait_seconds)
            for guard in slots:
                cleanup.callback(guard.close)
            return controls(args, slots)
    except ci.Refused as exc:
        print(f"rust_ci_control=refused reason={exc}", flush=True)
        return 75


if __name__ == "__main__":
    sys.exit(main())
