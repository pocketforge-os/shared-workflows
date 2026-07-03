# shared-workflows

Shared **reusable workflows** for org-wide PR checks in [pocketforge-os](https://github.com/pocketforge-os) — the peer-review merge gate today, plus future security/provenance/lint/CLA/static-analysis suites as they land.

Public repo so **every** repo in the org — private *or* public — can `uses:` the workflows here. (GitHub blocks a public repo from calling a `workflow_call` in a private repo, which is why this repo exists split out from `pocketforge-automation`.)

## What lives here

| Workflow | Purpose |
|---|---|
| `.github/workflows/pr-review-gate.yml` | Reusable — the 3-phase opencode peer-review gate (tsp-8ny). Runs on the `pf-pr-review` self-hosted lab runner. Exit 0 → check pass, 4 → blocking findings, 2 → workflow failure. |
| `.github/workflows/pr-review.yml` | Thin caller for **this** repo — dogfoods `pr-review-gate.yml` on its own PRs via the local `./` path. |

## Using it in another repo

Drop this thin caller into any pocketforge-os repo (keep the job id `pf-pr-review` — the org-wide required-check context is uniform):

```yaml
name: PR review

on:
  pull_request:
    types: [opened, reopened, synchronize, ready_for_review]

permissions:
  contents: read
  pull-requests: write

jobs:
  pf-pr-review:
    uses: pocketforge-os/shared-workflows/.github/workflows/pr-review-gate.yml@main
```

The `@main` reference is deliberately mutable — the gate re-fetches its review tooling from `pocketforge-automation` `origin/main` at run time regardless, so a SHA pin here doesn't pin behavior. Gate fixes must roll out org-wide without N repo bumps, and changes to the gate land through this repo's own branch → PR → review process (dogfooded by the local self-caller).

## Related

- `pocketforge-os/pocketforge-automation` — hosts `scripts/review-pr.sh` (the actual review runner), the self-hosted runner installer, and the automation the gate depends on at run time.
- Bead `tsp-mx3` — the org-wide required-check rollout that spun this repo out.
- Bead `tsp-8ny` — the original peer-review gate design.
