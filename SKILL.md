---
name: agent-ci-optimizer
description: >-
  Use when CI is slow, flaky or expensive, agents wait on checks, or someone
  asks for faster runners (Blacksmith, Depot, larger runners), Oxlint, bun
  check or TypeScript 7 tsc, Vitest sharding or isolate:false, cache or
  blobless-clone fixes, parallel jobs, affected tests (Nx, Turborepo), or
  local hooks, PR latency budgets, merge queues, full-suite scheduling, or
  release gates. Detect and design four validation categories for any repo
  and CI provider; evidence-first and read-only until authorized.
---

# Agent CI Optimizer

## Mission

Reduce **push → trustworthy green → merge → production** time, not only a job's elapsed time. Measure queueing, setup, checkout, dependency fetch, execution, critical path, retries, and approval wait. A false-green pipeline is a worse regression than a slow one.

This is an investigative skill, not blanket authorization to modify CI. **Start in OBSERVE.** Use the most local evidence available; every timing, cost, version and vendor claim comes from a measurement or a dated source.

### Modes

| Mode | Allowed action | Exit condition |
|---|---|---|
| `OBSERVE` (default) | Read code/config/logs, collect read-only metrics, prepare proposals | Baseline and trust boundaries documented |
| `EXPERIMENT` | When authorized, run a candidate in an isolated, non-production branch | Reproducible benchmark and safety results |
| `IMPLEMENT` | When authorized, change one intervention in a reviewable PR | Required checks pass; gain or policy trade-off evidenced |
| `MONITOR` | Read telemetry, flag regressions, propose rollback | Protected gates unchanged |

Buying runner capacity, connecting billing, changing branch protection or rulesets, merging, deploying, rotating secrets, deleting checks or tests, force-pushing, and writing to production each need **explicit authorization** for that action. Approval of an audit covers the audit only. Untrusted PR code never runs in a privileged context.

Reuse authorization already given in the conversation. A request to implement this skill or a concrete change authorizes that scoped work; do not repeatedly ask for the same permission. Resolve ordinary reversible implementation choices autonomously.

## Suitability

Activate modules only for the detected stack: JS/TS modules stay out of non-JS repos; affected-task tooling (Nx, Turborepo) only where a project graph exists or the benefit is measured; provider migrations only on this repo's benchmark, never a vendor's. A clear `NOT APPLICABLE` beats speculative tooling.

## Design four validation categories first

Read [validation architecture](references/08-validation-architecture.md) before proposing test selection, gate movement or a CI redesign. Classify actual commands and guarantees, not job names; one job can contain multiple categories.

| Category | Question | Default design to evaluate |
|---|---|---|
| `rules` | Does the code follow our rules? | PR lint, formatting and compilation/type checks; fast equivalents in pre-commit/pre-push hooks |
| `change` | Does this change work? | Edited tests **and** affected tests/dependents, contracts and risk-specific checks on every PR; FULL when confidence is missing |
| `integration` | Does integrated main work? | Full verification on merge candidates, or an explicitly accepted post-push rolling policy; retain verified commit/artifact records |
| `release` | Is the product ready to release? | Promote only verified inputs through product-appropriate staging, compatibility and telemetry gates; progressive rollout where supported |

Detect the provider, product type, commands, graph, triggers, protected checks, deployment model and telemetry. Use discovery's `validation_topology` only as evidence pointers; unresolved categories/policies stay `unknown`. Produce `validation-plan.md` with current → proposed placement, triggers, command/check identity, selection and fallback, confidence/source, budget, full-suite policy, release provenance, owners and rollback. Keep a cheap full PR suite when selection would add more risk or overhead.

Propose a configurable **300-second p95 PR feedback budget** unless the repo supplies one; it is a target, not an observed baseline or a timeout. Include queue/setup/retries and report approval wait separately. A completed run over budget triggers agent investigation; an agreed rolling breach triggers optimization, never deleted validation or a forced green. Report full-suite freshness, merge-queue latency and total runner use alongside PR latency. No automated recurring work is installed without a request.

## Entry checklist (before any change)

Script paths below are relative to this skill's directory; from the target repo, call them by absolute path. Write evidence to `REPO/.ci-audit/` (`mkdir -p` it first) and add `.ci-audit/` to `REPO/.git/info/exclude` so it is never committed. All scripts are Python 3.10+ stdlib; read each script's `--help` before running it.

1. Read repo policy (`AGENTS.md`, `CLAUDE.md`, CONTRIBUTING, CODEOWNERS), CI config, deployment and protected-branch requirements. Follow the user's scope and existing authorization; surface unresolved policy conflicts before the affected action. Before proposing a change to a line, read its history (`git log --follow -p`, the PR that added it): a recent deliberate choice is the owner's policy, not a bug.
2. Inventory: `python3 scripts/discover_repo.py --root REPO > REPO/.ci-audit/discovery.json`. It reads bounded regular `package.json` and GitHub workflow files, rejects file/directory symlinks, and inventories other files by name. Parsed config fields can still contain sensitive literals: keep evidence local and review before sharing. Verify each hint by reading approved files. Map evidence with the [discovery matrix](references/01-discovery-matrix.md) and build the four-category plan.
3. Identify CI provider(s), runner trust domains, workflows and job `needs`, languages, lockfiles, monorepo graph, test runners, coverage gates, deployment paths, and secret **names and permission scopes only**.
4. Identify permission boundaries: fork PR vs trusted branch vs merge queue vs release. GitHub collection needs authenticated `gh` with read access to Actions; benchmarking never justifies write scope.
5. Collect up to 150 recent runs into an immutable, hashed snapshot (failed runs included, kept distinguishable):
   `python3 scripts/collect_gh.py --repo OWNER/REPO --limit 150 --out REPO/.ci-audit`
   Add `--event pull_request --target-branch main`, or `--event push --branch main`, to separate loops. `--branch` filters history, not protected-policy scope. Use `--include-incomplete` to retain waiting/running observations, `--created`/`--workflow` for server filters, and `--incremental` for authenticated ETag revalidation. Exit 3 means explicit evidence gaps; inspect completeness, never reuse a failed payload. Other providers: [evidence formats](references/11-evidence-formats.md).
6. Analyze: `python3 scripts/analyze_gh.py --input REPO/.ci-audit --output REPO/.ci-audit/metrics.json`. It separates attempts, successful latency proxies, queue, overlap-aware setup, step distributions, recovery, partial reruns, runner seconds, estimated rounded minutes and uncertainty. Add `--graph`, `--cohort-context` and dated `--pricing` only with reviewed evidence. `--since` filters the entire run/job/attempt population; keep configuration eras separate.
   Inspect freshness, completeness, sample sizes and unresolved identities. These are diagnostic proxies. For PR budgets, full-suite freshness and exact artifact eligibility, use `evaluate_validation.py` with a reviewed policy and provider-normalized evidence; it does not authenticate sources or verify signatures. Missing/contradictory evidence cannot pass. See [evidence formats](references/11-evidence-formats.md).
7. Establish p50/p95 for push→green, workflow wall, queue, setup, lint, typecheck, tests, cache hit/miss, flaky retries, runner spend, cost per merge, and tail outliers. Missing or inapplicable metrics are **unknown**, never zero.
8. Read the [safety policy](references/03-security-correctness.md), [platform semantics](references/09-platform-updates.md) and relevant [stack adapters](references/10-stack-adapters.md). `inspect_workflows.py` accepts pinned-YAML-1.2-normalized JSON; expressions/reusable expansion and runtime identities remain unknown until resolved. Build the dependency DAG and name the slowest required path.

**Evidence rule:** every number in an audit carries source, time window, sample size, metric definition and limitations. Compare the same workflow, event and runner class; keep fork PRs apart from privileged release builds. A YAML pattern match is a lead, not a finding: a finding needs positive evidence from this repo's runs ([evidence tiers](references/04-measurement-benchmarking.md#evidence-tiers)).

**Two axes:** size every candidate on Δ wall-clock and Δ runner-minutes separately. Upstream jobs can shorten the critical path even when they finish before the last job; recompute the dependency graph. Distinguish execution acceleration from moving work between validation categories ([sizing](references/04-measurement-benchmarking.md#sizing-and-combining-savings)).

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
| **7. Local hooks** | Fast pre-commit format/lint; pre-push project checks when measured cheap | Reproducible on supported platforms, failure propagates, emergency escape hatch, **CI stays authoritative** (hooks are bypassable) |

### Strict safety invariants

- A green required check comes only from passing work: never from a hidden skip, `continue-on-error`, unconditional `|| true`, an unexpected `if`, missing tests, or a default-branch-only filter.
- Required check **names and identities** stay stable, or move explicitly with protected-branch owner approval. On GitHub a workflow skipped by `paths`/`branches` filters leaves its required check Pending; a job skipped by `if:` reports Success. Gate both through an aggregate job.
- `pull_request_target`, `workflow_run` and similar privileged triggers never execute fork code with secrets or write permissions. Releases and deployments run in isolated privileged jobs.
- Cache keys include every correctness input: lockfiles, toolchain versions, platform/ABI, build-relevant env and config. Trusted runs restore only caches trusted runs wrote.
- Without a correct base commit, dependency graph, coverage mapping, or selector confidence, run FULL. Change-to-check contracts cover schemas, generated code, migrations, security policies and CI files.
- Test isolation is removed per suite, opt-in, with evidence of no cross-file leakage; a timing target is never the reason.
- Category 3 must complete under the accepted merge-queue/post-push policy before a commit/artifact becomes release-eligible. Scheduled verification supplements it; a schedule alone does not certify untested commits. A latest green PR, mutable branch name or unchecked rebuild is insufficient release evidence.
- Actions and images are pinned and dependencies locked per repo policy; runners are ephemeral and least-privilege.
- Every optimization has one-variable attribution, a rollback plan, and monitoring.

## Operating workflow

### A. Diagnose and prioritize (no modifications)

1. Complete the four-category current/proposed map and policy decision, then reconstruct the DAG; separate queue, setup, steps and critical path. GitHub's jobs API gives timings; the `needs` edges come from YAML. Preserve existing gates while unknowns are resolved.
2. Fill the seven-module table: `applicable`, `observed bottleneck`, `Δ wall-clock` (on path or below floor), `Δ runner-minutes`, `evidence tier`, `correctness/security risk`, `effort`, `cost delta`, `next proof`.
3. Order by **risk-adjusted saving on the required path**: reversible setup/cache fixes before runner or provider changes. [Measurement guide](references/04-measurement-benchmarking.md).
4. Run the [pre-publish review](references/03-security-correctness.md#before-publishing-a-report), then deliver the report and proposed sequence. If scoped implementation is already authorized, continue; otherwise stop before the unapproved action.

### B. Benchmark one change

1. Freeze baseline commit, toolchain versions, runner architecture and config. Measure warm cache, cold cache and queue separately.
2. Run ≥10 comparable repetitions; low-sample tails remain preliminary. `benchmark.py` runs **exactly** the argv after `--`, with no shell:
   `python3 scripts/benchmark.py --label baseline --runs 10 --metadata REPO/.ci-audit/benchmark-context.json --out REPO/.ci-audit/experiments/baseline.json -- npm test`
   Compare with `--baseline`, or use reviewed JSON `--candidate-command` for balanced paired trials. Declare workload/toolchain/cache/runner identity; capture source before and after. Failed warmups, source mutation, incomplete process-group cleanup or missing comparability invalidate a claim. POSIX process groups are cleanup, not a sandbox. Review commands for side effects: deployments, payments, migrations and destructive operations are never benchmarked.
3. Compare p50/p95 wall time, end-to-end required-check time, spend per merge, cache restores, flakes, diagnostics, tests run, coverage, builds and artifacts. A correctness or security regression rejects the change whatever the speed-up.
4. Selector changes: run `scripts/select_affected.py` against adversarial diffs (lockfile, root config, CI file, schema, migration, cross-package rename); it is a reference oracle, not a substitute for the build tool's graph.
5. Record the experiment and decide `ADOPT`, `REJECT`, or `INSUFFICIENT EVIDENCE`.

### C. Implement and verify

- **One independently reversible PR per intervention.** Runner migration, test selection, test isolation and permission changes each ship alone. Use the [optimization PR template](templates/optimization-pr.md); config starting points are in [recipes](references/07-config-recipes.md).
- Keep existing check names and required gates. Run full relevant verification on the change, and prove each changed gate goes red on a probe commit.
- Workflow permissions or cache trust boundaries changed → security-owner review. Cost, vendor or trust jurisdiction changed → project-owner review. Coverage, isolation or affected selection changed → test-owner review plus mutation checks.
- Success is claimed from post-merge CI telemetry, not local benchmarks: reruns, workload state and gate migration per [CI-side verification](references/04-measurement-benchmarking.md#ci-side-verification).
- Gate redistribution: shadow selected vs full results, retain the full-suite oracle, prove event-specific aggregates and exact release provenance, and account for risk/latency transferred to integration. A smaller PR workload is not a same-work speed-up.

### D. Monitor, adapt, roll back

Track p50/p95 push→green, queue/setup breakdown, p95 critical path, cache efficiency, reruns, skipped-vs-full selection, cost per merged PR, false greens, escaped defects and release provenance. Re-assess after each change: bottlenecks move. When another job becomes the gate, report the new gate, not the old job's gain as wait removed. Investigation triggers (not universal gates): queue p95 >60 s, setup >25% of the critical job, slowest shard >1.5× median, flake >5%, p95 >2× target. Rollback triggers: [measurement guide](references/04-measurement-benchmarking.md#monitoring-and-rollback).

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

The report states the detected stack, four-category plan, PR budget, full-suite and release contracts, top bottleneck, seven-module status, risks, baselines, confidence and limits, ordered PR plan, acceptance criteria, no-change findings, cost trade-offs and rollback. Template: [audit report](templates/audit-report.md). Worked shapes: [examples](references/05-worked-examples.md).

## Stop and escalate

If authorization is absent, stop before changing paid runners/providers, secrets, release policy, protected checks, privileged execution, test coverage, security scanning, major runtime/build versions or production infrastructure. Explain the specific boundary. Never install hooks from untrusted code. When data are missing, report the gap instead of a gain.

## Resources

- [Discovery matrix](references/01-discovery-matrix.md) — stack → modules, CI-provider adapters, workflow risk flags.
- [Seven-module playbook](references/02-optimization-playbook.md) — experiments, counterexamples, gates, verified tool facts.
- [Security and correctness](references/03-security-correctness.md) — trust zones, skipped-check semantics, failure testing, pre-publish review.
- [Measurement and benchmarking](references/04-measurement-benchmarking.md) — metric definitions, two-axis sizing, evidence tiers, experiments, CI-side verification, rollback, raw schema.
- [Worked examples](references/05-worked-examples.md) — monorepo, small library, polyglot, agent-heavy repo.
- [Official sources](references/06-official-sources.md) — dated URLs, plus claims not yet confirmed.
- [Config recipes](references/07-config-recipes.md) — aggregate check, caches, blobless clone, Oxlint, typecheck, Vitest projects, hooks.
- [Validation architecture](references/08-validation-architecture.md) — four categories, evidence-driven design, PR budget, merge queue vs rolling main, release eligibility and rollout gates.
- [Platform semantics](references/09-platform-updates.md) — parallel steps, cache capabilities, lockfiles, queues, approval, retention and runners.
- [Stack adapters](references/10-stack-adapters.md) — Go, Next.js, Vitest, typed lint, formatting, Docker, Playwright, remote caches and artifacts.
- [Evidence formats](references/11-evidence-formats.md) — snapshot, DAG, benchmark and four-category evaluator contracts; [feature matrix](references/feature-matrix.json) records support and verification scope.
- [Agent brief](templates/agent-prompt.md) — paste-ready prompt for agents without this skill.

Self-test: `python3 -m unittest discover -s tests -v` from this skill's directory.
