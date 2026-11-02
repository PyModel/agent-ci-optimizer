---
name: agent-ci-optimizer
description: >-
  Use when CI is slow, flaky or expensive, agents wait on checks, or someone
  asks for faster runners (Blacksmith, Depot, larger runners), Oxlint, bun
  check or TypeScript 7 tsc, Vitest sharding or isolate:false, cache or
  blobless-clone fixes, parallel jobs, affected tests (Nx, Turborepo), or
  pre-push hooks. Evidence-first audit of push-to-green for any repo and CI
  provider; read-only until approved.
---

# Agent CI Optimizer

## Mission

Reduce **push → trustworthy green → merge → production** time, not only a job's elapsed time. Measure queueing, setup, checkout, dependency fetch, execution, critical path, retries, and approval wait. A false-green pipeline is a worse regression than a slow one.

This is an investigative skill, not blanket authorization to modify CI. **Start in OBSERVE.** Use the most local evidence available; every timing, cost, version and vendor claim comes from a measurement or a dated source.

### Modes

| Mode | Allowed action | Exit condition |
|---|---|---|
| `OBSERVE` (default) | Read code/config/logs, collect read-only metrics, prepare proposals | Baseline and trust boundaries documented |
| `EXPERIMENT` | With approval, run a candidate in an isolated, non-production branch | Reproducible benchmark and safety results |
| `IMPLEMENT` | With approval, change one intervention in a reviewable PR | All required checks pass, measured gain |
| `MONITOR` | Read telemetry, flag regressions, propose rollback | Protected gates unchanged |

Buying runner capacity, connecting billing, changing branch protection or rulesets, merging, deploying, rotating secrets, deleting checks or tests, force-pushing, and writing to production each need **explicit authorization** for that action. Approval of an audit covers the audit only. Untrusted PR code never runs in a privileged context.

## Suitability

Activate modules only for the detected stack: JS/TS modules stay out of non-JS repos; affected-task tooling (Nx, Turborepo) only where a project graph exists or the benefit is measured; provider migrations only on this repo's benchmark, never a vendor's. A clear `NOT APPLICABLE` beats speculative tooling.

## Entry checklist (before any change)

Script paths below are relative to this skill's directory; from the target repo, call them by absolute path. Write evidence to `REPO/.ci-audit/` (`mkdir -p` it first) and add `.ci-audit/` to `REPO/.git/info/exclude` so it is never committed. All scripts are Python 3.10+ stdlib; read each script's `--help` before running it.

1. Read repo policy (`AGENTS.md`, `CLAUDE.md`, CONTRIBUTING, CODEOWNERS), CI config, deployment and protected-branch requirements. Record conflicts with this skill; the stricter safety rule wins.
2. Inventory: `python3 scripts/discover_repo.py --root REPO > REPO/.ci-audit/discovery.json`. It opens only `package.json` (script and dependency names) and GitHub workflow files (risk-pattern lines, secret names); everything else, `.env` included, is seen by name alone. Verify each finding by reading the file. Map findings to modules with the [discovery matrix](references/01-discovery-matrix.md).
3. Identify CI provider(s), runner trust domains, workflows and job `needs`, languages, lockfiles, monorepo graph, test runners, coverage gates, deployment paths, and secret **names and permission scopes only**.
4. Identify permission boundaries: fork PR vs trusted branch vs merge queue vs release. GitHub collection needs authenticated `gh` with read access to Actions; benchmarking never justifies write scope.
5. Collect up to 150 recent completed runs (failed runs included, kept distinguishable):
   `python3 scripts/collect_gh.py --repo OWNER/REPO --limit 150 --out REPO/.ci-audit`
   Add `--event pull_request` or `--event push --branch main` to separate loops. Exit 3 means some runs lack job data: metrics are partial, say so. Other providers: normalize to the [raw schema](references/04-measurement-benchmarking.md#raw-schema).
6. Analyze: `python3 scripts/analyze_gh.py --input REPO/.ci-audit --output REPO/.ci-audit/metrics.json`. It reports push→green, run wall time, job queue, setup share, shard skew, reruns, flaky SHAs, last-finishing job, and billable minutes per runner label. It does not know required checks or the `needs` DAG; join `raw/required_checks.json` and the YAML yourself.
7. Establish p50/p95 for push→green, workflow wall, queue, setup, lint, typecheck, tests, cache hit/miss, flaky retries, runner spend, cost per merge, and tail outliers. Missing or inapplicable metrics are **unknown**, never zero.
8. Read the [safety policy](references/03-security-correctness.md). Build the `needs` DAG and name the slowest required path.

**Evidence rule:** every number in an audit carries source, time window, sample size, metric definition and limitations. Compare the same workflow, event and runner class; keep fork PRs apart from privileged release builds.

## Seven mandatory optimization modules

**Evaluate every module; activate only those the stack and measurements support.** Recipes, counterexamples and verified tool facts: [optimization playbook](references/02-optimization-playbook.md).

| Module | Candidate experiment | MUST pass before adoption |
|---|---|---|
| **1. Fast runners** | Benchmark stock, larger, and approved third-party ephemeral runners (Blacksmith, Depot) on the same commit and workload | Comparable p50/p95 queue + total time; price, concurrency limits, isolation, data residency, trust boundary and cancellation verified; explicit spend approval |
| **2. Native lint/typecheck** | JS/TS only: Oxlint vs ESLint; `bun check --no-script` or TypeScript 7 `tsc` vs current `tsc` | Rules, plugins, ignores and diagnostics diffed; missing rules kept in an ESLint fallback; declaration emit and build gates retained; enforcement equal or stronger |
| **3. Bigger machines / Vitest reuse** | Matrix of runner CPU × shards × workers; `isolate: false` only for independently safe Vitest projects | Full suite green under shuffled order, global-state leak checks, same coverage, stable flake rate; isolation kept for stateful/integration suites |
| **4. Caching / blobless clone** | Lockfile-aware cache keys, scoped cache writes, less setup; `filter: blob:none` with the history affected tools need | Cold path correct; restore cheaper than rebuild; untrusted runs cannot write caches trusted runs restore; diff/affected/history tools still work |
| **5. Parallel jobs** | Run independent lint, types, build, test concurrently; drop unneeded `needs` | One aggregate required check still fails on any child failure; no oversubscription, setup explosion, or hidden ordering |
| **6. Affected tasks** | Nx affected / Turborepo / Bazel / Pants / Gradle **only with a project graph**; [fail-closed selector](scripts/select_affected.py) as fallback and test oracle | Correct merge-base, graph coverage, transitive dependents, config/contract triggers, adversarial diffs; unknown inputs force FULL |
| **7. Pre-push hooks** | Fast changed-file format/lint and project-level typecheck; agent-friendly local feedback | Reproducible on supported platforms, failure propagates, emergency escape hatch, **CI stays authoritative** (hooks are bypassable) |

### Strict safety invariants

- A green required check comes only from passing work: never from a hidden skip, `continue-on-error`, unconditional `|| true`, an unexpected `if`, missing tests, or a default-branch-only filter.
- Required check **names and identities** stay stable, or move explicitly with protected-branch owner approval. On GitHub a workflow skipped by `paths`/`branches` filters leaves its required check Pending; a job skipped by `if:` reports Success. Gate both through an aggregate job.
- `pull_request_target`, `workflow_run` and similar privileged triggers never execute fork code with secrets or write permissions. Releases and deployments run in isolated privileged jobs.
- Cache keys include every correctness input: lockfiles, toolchain versions, platform/ABI, build-relevant env and config. Trusted runs restore only caches trusted runs wrote.
- Without a correct base commit, dependency graph, coverage mapping, or selector confidence, run FULL. Change-to-check contracts cover schemas, generated code, migrations, security policies and CI files.
- Test isolation is removed per suite, opt-in, with evidence of no cross-file leakage; a timing target is never the reason.
- Full verification runs on a schedule and before production whenever PR selection alone cannot prove equivalent safety.
- Actions and images are pinned and dependencies locked per repo policy; runners are ephemeral and least-privilege.
- Every optimization has one-variable attribution, a rollback plan, and monitoring.

## Operating workflow

### A. Diagnose and prioritize (no modifications)

1. Reconstruct the DAG from provider config; separate queue, setup, steps and critical path. GitHub's jobs API gives timings; the `needs` edges come from YAML.
2. Fill the seven-module table: `applicable`, `observed bottleneck`, `expected critical-path saving`, `confidence`, `correctness/security risk`, `effort`, `cost delta`, `next proof`.
3. Order by **risk-adjusted saving on the required path**: reversible setup/cache fixes before runner or provider changes. [Measurement guide](references/04-measurement-benchmarking.md).
4. Deliver the `OBSERVE` report and proposed sequence. Material or paid changes wait for approval.

### B. Benchmark one change

1. Freeze baseline commit, toolchain versions, runner architecture and config. Measure warm cache, cold cache and queue separately.
2. Run ≥10 comparable repetitions, interleaving arms when noisy; fewer is `preliminary`. `benchmark.py` runs **exactly** the argv after `--`, with no shell:
   `python3 scripts/benchmark.py --label baseline --runs 10 --out REPO/.ci-audit/experiments/baseline.json -- npm test`
   then the candidate with `--baseline REPO/.ci-audit/experiments/baseline.json`; its `comparison.warnings` must be empty or explained. Review commands for side effects first: deploys, payments, data migrations and destructive operations are never benchmarked.
3. Compare p50/p95 wall time, end-to-end required-check time, spend per merge, cache restores, flakes, diagnostics, tests run, coverage, builds and artifacts. A correctness or security regression rejects the change whatever the speed-up.
4. Selector changes: run `scripts/select_affected.py` against adversarial diffs (lockfile, root config, CI file, schema, migration, cross-package rename); it is a reference oracle, not a substitute for the build tool's graph.
5. Record the experiment and decide `ADOPT`, `REJECT`, or `INSUFFICIENT EVIDENCE`.

### C. Implement and verify

- **One independently reversible PR per intervention.** Runner migration, test selection, test isolation and permission changes each ship alone. Use the [optimization PR template](templates/optimization-pr.md); config starting points are in [recipes](references/07-config-recipes.md).
- Keep existing check names and required gates. Run full relevant verification on the change, and prove each changed gate goes red on a probe commit.
- Workflow permissions or cache trust boundaries changed → security-owner review. Cost, vendor or trust jurisdiction changed → project-owner review. Coverage, isolation or affected selection changed → test-owner review plus mutation checks.
- Success is claimed from post-merge CI telemetry, not local benchmarks.

### D. Monitor, adapt, roll back

Track p50/p95 push→green, queue/setup breakdown, p95 critical path, cache efficiency, reruns, skipped-vs-full selection, cost per merged PR, false greens, escaped defects and release provenance. Re-assess after each change: bottlenecks move. Investigation triggers (not universal gates): queue p95 >60 s, setup >25% of the critical job, slowest shard >1.5× median, flake >5%, p95 >2× target. Rollback triggers: [measurement guide](references/04-measurement-benchmarking.md#monitoring-and-rollback).

## Mandatory output contract

```text
REPO/.ci-audit/             # in .git/info/exclude
  discovery.json
  raw/                      # provider API data; internal; never committed
  metrics.json
  ci-audit-report.md        # from templates/audit-report.md
  critical-path.md
  cache-audit.md
  flake-audit.md
  optimization-backlog.md
  experiments/*.json
  rollback-plan.md
```

The report states the detected stack, top bottleneck, seven-module status, risks, measured baselines, confidence and limits, ordered PR plan, acceptance criteria, no-change findings, cost trade-offs and a precise rollback per change. Template: [audit report](templates/audit-report.md). Worked shapes: [examples](references/05-worked-examples.md).

## Stop and escalate

Stop and ask before work that changes paid runners or providers, secrets, release policy, protected checks, privileged workflow execution, test coverage, security scanning, install hooks from untrusted code, major runtime/build versions, or production infrastructure. When data are missing, report the gap instead of a gain.

## Resources

- [Discovery matrix](references/01-discovery-matrix.md) — stack → modules, CI-provider adapters, workflow risk flags.
- [Seven-module playbook](references/02-optimization-playbook.md) — experiments, counterexamples, gates, verified tool facts.
- [Security and correctness](references/03-security-correctness.md) — trust zones, skipped-check semantics, failure testing.
- [Measurement and benchmarking](references/04-measurement-benchmarking.md) — metric definitions, experiments, rollback, raw schema.
- [Worked examples](references/05-worked-examples.md) — monorepo, small library, polyglot, agent-heavy repo.
- [Official sources](references/06-official-sources.md) — dated URLs, plus claims not yet confirmed.
- [Config recipes](references/07-config-recipes.md) — aggregate check, caches, blobless clone, Oxlint, typecheck, Vitest projects, hooks.
- [Agent brief](templates/agent-prompt.md) — paste-ready prompt for agents without this skill.

Self-test: `python3 -m unittest discover -s tests -v` from this skill's directory.
