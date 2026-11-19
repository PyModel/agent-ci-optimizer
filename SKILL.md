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

## Entry checklist

Read the [field guide](references/12-field-guide.md#entry-checklist-before-any-change)
for commands and the audit contract. Establish scope, repo policy and protected
checks; inventory the stack; map trust boundaries; collect immutable observations;
then separate queue, setup, work, retries and gates. Do not print raw logs or
sensitive config. Missing, inaccessible or incomparable data is **unknown**, never 0.

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
- Required check **names and identities** stay stable, or move explicitly with protected-branch owner approval. On GitHub, trigger required workflows for every protected candidate: an aggregate cannot run inside a filtered-out workflow. Gate conditional jobs through an event-aware aggregate; allow only explicitly proven intended skips.
- `pull_request_target`, `workflow_run` and similar privileged triggers never execute fork code with secrets or write permissions. Releases and deployments run in isolated privileged jobs.
- Cache keys include every correctness input: lockfiles, toolchain versions, platform/ABI, build-relevant env and config. Trusted runs restore only caches trusted runs wrote.
- Without a correct base commit, dependency graph, coverage mapping, or selector confidence, run FULL. Change-to-check contracts cover schemas, generated code, migrations, security policies and CI files.
- Test isolation is removed per suite, opt-in, with evidence of no cross-file leakage; a timing target is never the reason.
- Category 3 must complete under the accepted merge-queue/post-push policy before a commit/artifact becomes release-eligible. Scheduled verification supplements it; a schedule alone does not certify untested commits. A latest green PR, mutable branch name or unchecked rebuild is insufficient release evidence.
- Actions and images are pinned and dependencies locked per repo policy; runners are ephemeral and least-privilege.
- Every optimization has one-variable attribution, a rollback plan, and monitoring.

## Operating workflow

1. **Diagnose:** rank evidence-backed savings on the required path and total runner
   work separately; never add savings from overlapping jobs.
2. **Experiment:** pin source, workloads, inputs and environment; pair observations,
   compare cold/warm paths, retain failures and quantify uncertainty.
3. **Verify and ship:** one independently reversible intervention at a time, with
   equivalent checks/coverage, negative gate tests and the authorized review.
4. **Monitor:** confirm on real comparable CI, keep the full-suite oracle and exact
   release eligibility, and roll back on false green or a breached policy.

Follow the [field guide](references/12-field-guide.md#operating-workflow) and its
[output contract](references/12-field-guide.md#mandatory-output-contract). Load the
[measured adapters](references/13-measured-adapters.md) only for the claims being tested.
Keep helper outputs distinct from provider policy, quality signoff and release authorization.

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
