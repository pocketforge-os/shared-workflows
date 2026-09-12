#!/usr/bin/env python3
"""Exercise cohort admission with a live ordinary sibling, not an idle pool."""
import argparse
import json
import signal
from pathlib import Path
import subprocess
import sys
import tempfile

import live_rust_ci as live

CONTROLLER = live.CONTROLLER


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--image", required=True)
    args = p.parse_args()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, live.ci.interrupted)
    with tempfile.TemporaryDirectory(prefix="pf-rust-slot-contention-") as tmp:
        root = Path(tmp)
        source = root / "source"
        source.mkdir()
        (source / "normal.sh").write_text('''set -euo pipefail
printf keep > /work/normal-cache
touch /work/ready
while test ! -f /work/release; do sleep 0.2; done
test "$(cat /work/normal-cache)" = keep
echo normal_sibling=pass
''')
        (source / "quick.sh").write_text("echo spare_slot_normal=pass\n")
        live.checked("git", "init", "-q", str(source))
        live.checked("git", "-C", str(source), "add", ".")
        live.checked("git", "-C", str(source), "-c", "user.name=CI fixture", "-c",
                     "user.email=ci@example.invalid", "commit", "-qm", "ordinary sibling fixture")
        sha = live.checked("git", "-C", str(source), "rev-parse", "HEAD")
        command = [sys.executable, str(CONTROLLER), "--source", str(source), "--source-sha", sha,
                   "--platform", str(source), "--platform-sha", sha, "--image", args.image]
        logs = [root / "normal.log", root / "cohort.log"]
        children = []
        try:
            with logs[0].open("w") as stream:
                normal = subprocess.Popen(command + ["--script", "normal.sh"], stdout=stream,
                                          stderr=subprocess.STDOUT)
                children.append(normal)

            def identity():
                assert normal.poll() is None, logs[0].read_text()
                for line in logs[0].read_text().splitlines():
                    if line.startswith('{"rust_ci": "starting"'):
                        return json.loads(line)["name"]

            name = live.wait_for(identity, seconds=660)
            details = json.loads(live.checked("docker", "inspect", name))[0]
            work = Path(next(m["Source"] for m in details["Mounts"] if m["Destination"] == "/work"))
            live.wait_for(lambda: (work / "ready").exists())
            with logs[1].open("w") as stream:
                cohort = subprocess.Popen([sys.executable, str(Path(live.__file__)), "--image", args.image],
                                          stdout=stream, stderr=subprocess.STDOUT)
                children.append(cohort)
            live.wait_for(lambda: '"rust_ci": "waiting_for_slots", "count": 2' in logs[1].read_text())
            # Pair acquisition cannot hold the spare slot while the first is
            # busy: a second ordinary invocation still executes successfully.
            quick = subprocess.run(command + ["--script", "quick.sh"], capture_output=True, text=True, timeout=660)
            assert quick.returncode == 0 and "spare_slot_normal=pass" in quick.stdout, quick
            assert normal.poll() is None and cohort.poll() is None
            assert (work / "normal-cache").read_text() == "keep"
            (work / "release").touch()
            assert normal.wait(timeout=30) == 0, logs[0].read_text()
            assert cohort.wait(timeout=660) == 0, logs[1].read_text()
            assert "case=concurrent_cache_and_cancel_isolation" in logs[1].read_text()
            assert not work.parent.exists()
            print("rust_ci_control=pass case=ordinary_sibling_then_atomic_cohort spare_slot_preserved=true", flush=True)
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=30)
            for log in logs:
                if log.exists():
                    print(log.read_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
