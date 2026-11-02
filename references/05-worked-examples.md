# Worked examples

Illustrative shapes, not measurements. Numbers here show the reasoning; never copy them into a report.

## A. TS monorepo, 40-minute PR loop

**Observed:** pnpm + Nx, one `ci` job: install (4 min) → lint (6) → `tsc -b` (8) → vitest (20) → build (2). Queue p95 90 s. `setup_share` 0.15, no cache on `~/.pnpm-store`.

**Plan, in PR order:**
1. Cache pnpm store via `setup-node cache: pnpm` (module 4). Low risk, saves install on warm runs.
2. Split lint / typecheck / test / build into parallel jobs with an aggregate required check named like the old job (module 5).
3. Shard vitest 4 ways, rebalance until `shard_skew` < 1.3 (module 3).
4. `nx affected -t lint test typecheck` with `nx-set-shas`, `fetch-depth: 0` + `filter: blob:none`, daily full run on main and full on `merge_group` (module 6). Adversarial diffs must go FULL.
5. Oxlint first + ESLint with `eslint-plugin-oxlint` for type-aware and custom rules (module 2). Diagnostic diff on 3 commits.
6. Only now: benchmark larger runners against the new critical path (module 1); spend approval required.

**Rejected:** `isolate: false` globally (integration tests share a DB); TS 7 `tsc` until Vue SFC typecheck has a TS 7 path.

## B. Small Python library, 4-minute CI

**Observed:** uv, pytest on 3 Python versions × 2 OS. Install 50 s of 3 min per job, no concurrency group.

**Plan:** `concurrency: {group: ci-${{ github.ref }}, cancel-in-progress: true}` for PR refs only; uv cache via `astral-sh/setup-uv` cache option; pre-push ruff hook. Everything else **NOT APPLICABLE**: no graph, runners not the bottleneck, already parallel. Saying so is the deliverable.

## C. Polyglot service (Go API + React UI + Terraform)

**Observed:** one workflow, `needs` chain go → ui → terraform plan, all required; `paths:` filter on the workflow.

**Findings:** the `paths:` filter on a required workflow leaves the check Pending on docs-only PRs (GitHub keeps skipped required workflows Pending), so people admin-merge. The chain is serial with no data dependency.

**Plan:** remove workflow-level `paths:`; use job-level change detection plus an aggregate job that treats intended skips as pass (module 5 + [skipped checks](03-security-correctness.md#required-checks-and-skips)); `terraform plan` stays in its own job with OIDC and no fork access; Go build cache via `setup-go` (module 4).

## D. Agent-heavy repo

**Observed:** coding agents push every few minutes; 60% of runs cancelled or superseded; flake rate 8% on e2e.

**Plan:** `cancel-in-progress` on PR refs; pre-push hook running the exact CI lint/type commands (module 7) so agents see red locally; quarantine list for known flaky e2e **with an owner and expiry**, tracked in `flake-audit.md`, never silent skips; e2e moved to `merge_group` + nightly if product owners accept the latency trade.
