# Audit field guide

Load when conducting an audit or preparing a scoped optimization PR.

## Entry checklist (before any change)

Script paths below are relative to this skill's directory; from the target repo, call them by absolute path. Write evidence to `REPO/.ci-audit/` (`mkdir -p` it first) and add `.ci-audit/` to `REPO/.git/info/exclude` so it is never committed. All scripts are Python 3.10+ stdlib; read each script's `--help` before running it.

1. Read repo policy (`AGENTS.md`, `CLAUDE.md`, CONTRIBUTING, CODEOWNERS), CI config, deployment and protected-branch requirements. Follow the user's scope and existing authorization; surface unresolved policy conflicts before the affected action. Before proposing a change to a line, read its history (`git log --follow -p`, the PR that added it): a recent deliberate choice is the owner's policy, not a bug.
2. Inventory: `python3 scripts/discover_repo.py --root REPO > REPO/.ci-audit/discovery.json`. It reads bounded regular `package.json` and GitHub workflow files, rejects file/directory symlinks, and inventories other files by name. Parsed config fields can still contain sensitive literals: keep evidence local and review before sharing. Verify each hint by reading approved files. Map evidence with the [discovery matrix](01-discovery-matrix.md) and build the four-category plan.
3. Identify CI provider(s), runner trust domains, workflows and job `needs`, languages, lockfiles, monorepo graph, test runners, coverage gates, deployment paths, and secret **names and permission scopes only**.
4. Identify permission boundaries: fork PR vs trusted branch vs merge queue vs release. GitHub collection needs authenticated `gh` with read access to Actions; benchmarking never justifies write scope.
5. Collect up to 150 recent runs into an immutable, hashed snapshot (failed runs included, kept distinguishable):
   `python3 scripts/collect_gh.py --repo OWNER/REPO --limit 150 --out REPO/.ci-audit`
   Add `--event pull_request --target-branch main`, or `--event push --branch main`, to separate loops. `--branch` filters history, not protected-policy scope. Use `--include-incomplete` to retain waiting/running observations, `--created`/`--workflow` for server filters, and `--incremental` for authenticated ETag revalidation. Exit 3 means explicit evidence gaps; inspect completeness, never reuse a failed payload. For GitLab native data and other evidence tools, see [measured adapters](13-measured-adapters.md).
6. Analyze: `python3 scripts/analyze_gh.py --input REPO/.ci-audit --output REPO/.ci-audit/metrics.json`. It separates attempts, successful latency proxies, queue, overlap-aware setup, step distributions, recovery, partial reruns, runner seconds, estimated rounded minutes and uncertainty. Add `--graph`, `--cohort-context` and dated `--pricing` only with reviewed evidence. `--since` filters the entire run/job/attempt population; keep configuration eras separate.
   Inspect freshness, completeness, sample sizes and unresolved identities. These are diagnostic proxies. For PR budgets, full-suite freshness and exact artifact eligibility, use `evaluate_validation.py` with a reviewed policy and provider-normalized evidence; it does not authenticate sources or verify signatures. Missing/contradictory evidence cannot pass. See [evidence formats](11-evidence-formats.md).
7. Establish p50/p95 for push→green, workflow wall, queue, setup, lint, typecheck, tests, cache hit/miss, flaky retries, runner spend, cost per merge, and tail outliers. Missing or inapplicable metrics are **unknown**, never zero.
8. Read the [safety policy](03-security-correctness.md), [platform semantics](09-platform-updates.md) and relevant [stack adapters](10-stack-adapters.md). `inspect_workflows.py` accepts pinned-YAML-1.2-normalized JSON; expressions/reusable expansion and runtime identities remain unknown until resolved. Build the dependency DAG and name the slowest required path.

**Evidence rule:** every number in an audit carries source, time window, sample size, metric definition and limitations. Compare the same workflow, event and runner class; keep fork PRs apart from privileged release builds. A YAML pattern match is a lead, not a finding: a finding needs positive evidence from this repo's runs ([evidence tiers](04-measurement-benchmarking.md#evidence-tiers)).

**Two axes:** size every candidate on Δ wall-clock and Δ runner-minutes separately. Upstream jobs can shorten the critical path even when they finish before the last job; recompute the dependency graph. Distinguish execution acceleration from moving work between validation categories ([sizing](04-measurement-benchmarking.md#sizing-and-combining-savings)).

## Operating workflow

### A. Diagnose and prioritize (no modifications)

1. Complete the four-category current/proposed map and policy decision, then reconstruct the DAG; separate queue, setup, steps and critical path. GitHub's jobs API gives timings; the `needs` edges come from YAML. Preserve existing gates while unknowns are resolved.
2. Fill the seven-module table: `applicable`, `observed bottleneck`, `Δ wall-clock` (on path or below floor), `Δ runner-minutes`, `evidence tier`, `correctness/security risk`, `effort`, `cost delta`, `next proof`.
3. Order by **risk-adjusted saving on the required path**: reversible setup/cache fixes before runner or provider changes. [Measurement guide](04-measurement-benchmarking.md).
4. Run the [pre-publish review](03-security-correctness.md#before-publishing-a-report), then deliver the report and proposed sequence. If scoped implementation is already authorized, continue; otherwise stop before the unapproved action.

### B. Benchmark one change

1. Freeze baseline commit, toolchain versions, runner architecture and config. Measure warm cache, cold cache and queue separately.
2. Run ≥10 comparable repetitions; low-sample tails remain preliminary. `benchmark.py` runs **exactly** the argv after `--`, with no shell:
   `python3 scripts/benchmark.py --label baseline --runs 10 --metadata REPO/.ci-audit/benchmark-context.json --out REPO/.ci-audit/experiments/baseline.json -- npm test`
   Compare with `--baseline`, or use reviewed JSON `--candidate-command` for balanced paired trials. Declare workload/toolchain/cache/runner identity; capture source before and after. Failed warmups, source mutation, incomplete process-group cleanup or missing comparability invalidate a claim. POSIX process groups are cleanup, not a sandbox. Review commands for side effects: deployments, payments, migrations and destructive operations are never benchmarked.
3. Compare p50/p95 wall time, end-to-end required-check time, spend per merge, cache restores, flakes, diagnostics, tests run, coverage, builds and artifacts. A correctness or security regression rejects the change whatever the speed-up.
4. Selector changes: run `scripts/select_affected.py` against adversarial diffs (lockfile, root config, CI file, schema, migration, cross-package rename); it is a reference oracle, not a substitute for the build tool's graph.
5. Record the experiment and decide `ADOPT`, `REJECT`, or `INSUFFICIENT EVIDENCE`.

### C. Implement and verify

- **One independently reversible PR per intervention.** Runner migration, test selection, test isolation and permission changes each ship alone. Use the [optimization PR template](../templates/optimization-pr.md); config starting points are in [recipes](07-config-recipes.md).
- Keep existing check names and required gates. Run full relevant verification on the change, and prove each changed gate goes red on a probe commit.
- Workflow permissions or cache trust boundaries changed → security-owner review. Cost, vendor or trust jurisdiction changed → project-owner review. Coverage, isolation or affected selection changed → test-owner review plus mutation checks.
- Success is claimed from post-merge CI telemetry, not local benchmarks: reruns, workload state and gate migration per [CI-side verification](04-measurement-benchmarking.md#ci-side-verification).
- Gate redistribution: shadow selected vs full results, retain the full-suite oracle, prove event-specific aggregates and exact release provenance, and account for risk/latency transferred to integration. A smaller PR workload is not a same-work speed-up.

### D. Monitor, adapt, roll back

Track p50/p95 push→green, queue/setup breakdown, p95 critical path, cache efficiency, reruns, skipped-vs-full selection, cost per merged PR, false greens, escaped defects and release provenance. Re-assess after each change: bottlenecks move. When another job becomes the gate, report the new gate, not the old job's gain as wait removed. Investigation triggers (not universal gates): queue p95 >60 s, setup >25% of the critical job, slowest shard >1.5× median, flake >5%, p95 >2× target. Rollback triggers: [measurement guide](04-measurement-benchmarking.md#monitoring-and-rollback).

Check the agreed PR budget and category-3 completion/freshness independently. Coalescing main runs must not cancel every full suite forever. A failed full run blocks eligibility for that commit; follow the repo's fix-forward/revert policy and incident owner. Missing rollout telemetry pauses promotion.

## Mandatory output contract

```text
REPO/.ci-audit/             # in .git/info/exclude
  discovery.json
  validation-plan.md        # four categories, evidence, budget and accepted policy
  current.json              # atomic pointer to latest collection snapshot
  snapshots/<id>/raw/        # immutable provider payloads + hash/completeness manifest
  metrics.json
  validation-policy.json    # reviewed expectations and fingerprinted policy
  validation-evidence.json  # authenticated/verified provider facts, normalized
  validation-result.json    # budget, integration, release and rollout decisions
  ci-audit-report.md        # from templates/audit-report.md
  critical-path.md
  cache-audit.md
  flake-audit.md
  optimization-backlog.md
  experiments/*.json
  rollback-plan.md
```

The report states the detected stack, four-category plan, PR budget, full-suite and release contracts, top bottleneck, seven-module status, risks, baselines, confidence and limits, ordered PR plan, acceptance criteria, no-change findings, cost trade-offs and rollback. Template: [audit report](../templates/audit-report.md). Worked shapes: [examples](05-worked-examples.md).
