# Public Rust container workflow v1

Call `.github/workflows/rust-container.yml@<full-commit-sha>` at job level, with
`contents: read`, `source-sha: ${{ github.sha }}`, the **same** commit in
`workflow-sha`, a `10.0.32.86:5555/pocketforge/ci-rust@sha256:<64-hex>` image and a
committed `script: scripts/ci-container.sh`. The script runs in `/work/source`.
Do not pass secrets/inherit, host Cargo/Rust paths, privileged options or runner
names. Same-org public and private callers are supported. The public workflow
checks out its public helper, not the private automation image-source repository.

`platform-ref` defaults to `main`, deliberately matching runtime's existing live
fixture regression gate. It resolves once, then both source and platform SHAs
are checked and archived without `.git`/credentials. Pass the recorded exact
platform SHA to replay. PR source is the synthetic merge (`github.sha`), not a
moving pull ref or just head. Source/image/platform/runner identities are logged.
Any `source-sha` different from `github.sha` is refused before checkout.

The payload receives `PF_SOURCE_SHA`, `PF_PLATFORM_SHA`, `PF_PLATFORM_DIR`,
`PF_IMAGE` and `SOURCE_DATE_EPOCH`. All task dependencies come from the image;
no host environment forwarding. Host responsibility is Actions/Git/HTTPS CA,
Bash/coreutils, Docker and Python 3 standard library for engine/admission.
The workflow poisons host Rust/Cargo/rustup/sccache/native/cross compiler commands
before invoking the real controller. Networking is ordinary container network;
the image sets Cargo offline and the caller keeps `--offline --locked` flags.

## Scheduling and retention

`[self-hosted, pf-ci-container]` selects the existing ordinary pool. Two lifetime
slot locks and on-disk reservations protect concurrent admission. Per run:
2 CPUs, 4 GiB memory/no extra swap, 512 PIDs, 256 MiB tmpfs, 8 GiB disk admission
budget (including source archives and outputs), private 512 MiB sccache. The
controller checks both Docker and output backing filesystems, inodes and memory;
it counts outstanding reservations conservatively and monitors run output size.
Disk is admission/monitor accounting, **not** a hard filesystem quota.

No shared Cargo target/registry or persistent cache is used, so simultaneous
writers of the same compiler key cannot clobber each other. No unbounded cache
key fanout/export or blanket prune exists. Cancellation removes only the random
container/directory belonging to the invocation. Engine cleanup uncertainty
retains its reservation/data for infrastructure reconciliation. SIGKILL/host
loss needs the same prove-job-ended orphan procedure as the existing CI pilot;
there is no TTL deletion of possibly active work. Source worktrees and published
referenced images are retained for parent review/release.

Before provisioning a new guest/expanding labels, reserve physical qcow2 backing
through automation's `ci-rust/reserve-runner.py` / .15 ledger, verify engine
service-user access and real controls. Do not increase slots/limits to evade a
refusal. The current modelmaker review pool has a retained 24 GiB backing envelope.
Image construction uses separately admitted existing OCI builders. This workflow
does not select the OS-image builder or interrupt model/build services.

## Controls and staged adoption

`python3 tests/test_rust_ci.py` covers byte/inode/memory floors, outstanding
reservation double-spend, both filesystems, dirty/wrong identity and unpinned
image refusal. `verify-controls: true` additionally runs real Docker missing
cargo and two concurrent same-key compiler-cache jobs, SIGTERMs one and verifies
the survivor's cache/output and exact-container cleanup. Enable this during
profile validation (automation's image-profile CI does so); ordinary caller
jobs stay parallel without each requesting a second slot for controls.

GitHub prefixes reusable checks with caller/callee names. Runtime retains its
original check name as a fail-closed dependent result gate; the full test payload
runs in the reusable job, and a failure/cancellation/skip cannot green that gate.
Keep caller path coverage and same-repo PR guard. No permission/ruleset changes
are needed: the org already allows SHA-pinned `pocketforge-os/*` workflows.

Merge image-source automation and this workflow before runtime adoption. Keep
the tested candidate commit pins reachable using merge commits; do not delete
the source branch until reviewed release. If the parent chooses merge-result
SHAs instead, repin `uses` **and** `workflow-sha`, then rerun caller CI. The image
digest continues to identify committed image-source bytes even after doc updates.
Subsequent launcher/GPU/infra consumers must inventory their additional packages,
publish the matching profile, preserve their actual test scripts/checks, and use
this same pin/identity/guard recipe. This v1 closure supports runtime, not all Rust
or device jobs. See automation `ci-rust/README.md` for exact inventory/remaining work.
