# Strict performance contract

Reviewed 2026-11-20. Apply this contract before older examples. These are optimization
requirements, not vendor guarantees or measurements. See [tool migration](15-toolchain-migration.md)
for current commands and compatibility traps; [sources](06-official-sources.md) records primary evidence.

## Contents

- [Acceptance and audit](#acceptance-and-audit)
- [Queue and cancellation](#queue-and-cancellation)
- [Runner and setup changes](#runner-and-setup-changes)
- [Parallel work and test reuse](#parallel-work-and-test-reuse)
- [Selection negative control](#selection-negative-control)
- [Local enforcement](#local-enforcement)
- [Budget enforcement](#budget-enforcement)
- [Small PRs and cost](#small-prs-and-cost)

## Acceptance and audit

Require first-attempt PR feedback p50 <120 s and p95 <180 s. Require per-job waiting
<10 s, setup <30 s, lint <5 s, typecheck <5 s, test work <60 s per shard and the
local pre-push path <9 s. Require >95% exact PR dependency-cache hits where that
cache is applicable. Define build, formatting, reporting and total-job budgets
from the measured path before modifying them; do not leave unbounded work.

Retrieve the last 150 runs, all paginated jobs and attempts, including failures,
cancellations and incomplete runs. Retention or fewer available runs limits the
sample; never fabricate the remainder. Split event, workflow/config era, runner,
cache state and first attempt versus rerun. Keep a failed original attempt even
when a later attempt passed. No check rerun substitutes for a code fix.

Report one row per job: waiting/setup/running p50 and p95, n, outcome counts,
timestamp source and gaps. Expose reporting/teardown/unattributed time. Missing
setup timestamps cannot count as a 0-second setup. Resolve step names against
actual commands; generic analyzer heuristics are observations, not budget proof.
Report dependency wait separately from job creation-to-start runner wait.

Rank the largest measured bucket on the required DAG. Do not sum parallel jobs
or their percentiles to estimate PR time. Record total runner seconds separately.
Use one stated percentile method across comparisons (the analyzer uses nearest-rank). A small sample's p95 is descriptive; one fast run cannot establish a reliable tail.
Measure current-revision feedback and the full PR journey including repair delay.

## Queue and cancellation

Enumerate the baseline suite, new tests, platform/database variants, build/type/coverage and migration obligations. Existing protection may omit required work; do not equate its current check list with the complete suite. Require that full suite on the actual combined queue candidate before main admission.
On GitHub, preserve the existing required aggregate identity and configure both:

```yaml
on:
  pull_request:
  merge_group:
    types: [checks_requested]
  push:
    branches: [main]
concurrency:
  group: ci-${{ github.workflow }}-${{ github.event_name }}-${{ github.event.pull_request.number || github.run_id }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
```

Use unique queue/main concurrency groups. A shared group can discard a pending
candidate even with `cancel-in-progress: false`. Cancel only superseded PR work;
release runners, processes and databases when cancelled.

Verify the provider rule requires the merge queue and full aggregate, and requires
every queue entry to pass. Preserve existing review/security requirements; remove
any bypass only within the authorized scope. Do not use admin bypass yourself.
Adding YAML does not activate a ruleset. Public organization repositories support
GitHub merge queues without an Enterprise upgrade; inspect actual repository eligibility.

Disable affected selection and task-result cache reads on the queue. Examples:

```sh
nx run-many -t test --skip-nx-cache
turbo run test --force
```

These force execution; they do not replace inventory, coverage or report checks.
Keep dependency/transform caches only where they cannot replay test conclusions.
Scheduled full runs supplement the queue. Post-push validation cannot protect main
retroactively. If activation is blocked, finish the code/PR and report the exact blocker.

## Runner and setup changes

Prefer Blacksmith for every repository-controlled job, including 5-second aggregates.
Install/verify its organization GitHub App access before changing labels. A runner
label is not proof of capacity. Verify images, architecture, isolation, fork trust,
permissions and cancellation; no workload goes to an unreviewed persistent runner.
Use 8-vCPU machines as the starting test-shard experiment, and smaller approved
machines for brief jobs. Measure queue plus total time; do not promise <10 s queues.

Seed dependency caches from trusted main. For JS, benchmark caching `node_modules`
itself, not only download stores. Require exact OS/image, arch, runtime ABI/version,
package-manager version, lockfile, workspace layout, install flags, trusted lifecycle
settings and patch/config compatibility. Do not restore a broad old dependency tree
as valid without a frozen reconciliation. Never put secrets in caches.

Count only primary-key exact restores in the >95% target; distinguish fallback,
miss, restore error and unknown. Include every eligible PR restore in the denominator.
Lockfile changes legitimately miss; report them, seed after trusted integration, and
never reuse incompatible dependencies to improve the rate. A cold path must work.
Trusted jobs must not consume artifacts or cache entries writable by untrusted PRs.

Use a full-history partial clone by default:

```yaml
with:
  filter: blob:none
  fetch-depth: 0
  persist-credentials: false
```

For direct Git use `git clone --filter=blob:none URL`; do not use `--depth=0`.
Measure deferred blob reads as part of the entire job, not only checkout. Keep shallow
or another strategy only with a measured reason and proof all required history exists.
Do not add dependency caches to a dependency-free repository merely to fill a checklist.

## Parallel work and test reuse

Run independent lint, types, build and tests concurrently. Keep genuine artifact or
schema dependencies. Move migration validation and unrelated application tests out
of the slowest job into independent required jobs; never beyond main admission.
Check the aggregate on failure, skip, cancellation, missing matrix legs and missing
reports. Avoid setup duplication that costs more than the parallelism saves.

Buy cores before more shards, within approved spend. Measure CPU, memory, I/O and
database contention. Allocate workers against actual available cores and memory;
leave capacity for services. Balance shards by measured file duration, preserve the
complete inventory, then randomize order within each shard with a recorded seed.
One database per shard still needs worker-level transaction/schema isolation.

For Vitest reuse, prove executable cleanup after each file before `isolate: false`:
restore mocks/timers/globals/env, DOM/storage, listeners/servers/pending work,
database state and application singletons. Put the same contract in `AGENTS.md`.
Do not reload the entire application in cleanup and call that reuse. Run multiple
recorded seeds and individual-file leak probes. Keep isolation only for documented
stateful/unsupported cases; fix the leak instead of adding retries or quarantine.

## Selection negative control

Use a real dependency graph and reviewed global inputs. Include edited tests,
transitive consumers, migrations, schemas/generated clients, shared fixtures,
lockfiles and runtime/compiler/CI configuration. A missing base, graph edge,
malformed result or unexplained empty selection forces FULL or a failed guard.

Prove the failure path with actual execution in a disposable branch/fixture:

1. Record a clean baseline and complete test inventory.
2. Change a dependency so a downstream test genuinely fails.
3. Deliberately hide that dependency edge/change from the selector.
4. Execute the selected work and an independent unfiltered FULL oracle on the same
   revision/environment; retain test IDs, exit codes and reports.
5. Require the comparison/selection guard to fail when selection is green and FULL
   is red. Also prove complete selection catches the same defect directly.
6. Remove the deliberate defect in a new revision and retain the regression test.

Use `evaluate_shadow.py` to compare normalized evidence; invalid/incomplete evidence
must fail. A green selector alone is not a negative control, and invented JSON
outcomes do not prove actual command/transport wiring. Keep the real queue FULL.

## Local enforcement

Install an actual pre-push hook using the repository's existing hook manager, or a
reviewed executable hook with `core.hooksPath`. Preserve existing hooks. Verify its
installation, executable mode and failure propagation; documentation alone is not
enforcement. Keep installation reversible and report any local capability blocker.

Pass changed paths as NUL-delimited data/argv, never shell-expanded text. Handle
new branches, force pushes, renames/deletions and missing bases conservatively.
Run Oxfmt/Oxlint on changed applicable files and tsrs or the verified checker on
affected projects/dependents. Checking only changed TS files misses reverse consumers.
Require <9 s including process startup. CI repeats authoritative checks because Git
hooks can be bypassed; never use hook execution as proof of merge eligibility.

## Budget enforcement

Use a monotonic guard such as `run_budget.py --seconds 60 --label tests -- COMMAND`
for a command. It must execute once, preserve failures and fail at equality. On POSIX, supervise an owned process group; commands must not daemonize or escape it. Windows fallback controls only the direct child, so use an appropriate supervisor when descendant cleanup is required. It measures work duration, not waiting/setup or fleet p95.
Keep provider job timeouts bounded as a final containment limit; never widen them
to hide a regression. A timeout is red even if a signal handler exits zero.

Use explicit phase markers and provider timestamps for waiting/setup/job budgets.
Validate exact job/attempt/SHA identity, all pagination, expected inventory and
successful outcomes. Fail missing/malformed evidence; a zero-exit report writer is
not a passing evaluator. A live verifier cannot certify its own future completion:
mark that duration pending, guard its command, and audit its completion afterwards.
Queues with no assigned runner cannot self-timeout; monitor their age separately.

Evaluate rolling p50/p95 and cache targets from a sufficiently sized comparable
cohort without deleting slow/failed observations. Do not turn synthetic fixtures,
local timings, success-only samples or an unenforced JSON policy into a CI SLO claim.

## Small PRs and cost

Keep runner migration, tool replacement, selection and isolation as separate PRs.
Before each switch, list what old and new uniquely detect, any missing behavior,
versions, identical workload, warm/cold timings and real CI links. Explain every
retained legacy tool with measured incompatibility/cost; accept a newer faster
replacement only after the same evidence. Report incomplete evidence plainly.

Collect account/app activation, permissions, secrets and incremental spend in one
list after completing everything else. A normal Blacksmith runner migration does
not inherently require a PAT or a new application secret. Quote current rates,
remaining organization-wide allowance, billing granularity and monthly volume.

Estimate compute from **all billable job time**, including full queue runs, main
cache seeding and verification, then add storage/idle/optional services. Do not
multiply the critical path by one price. Public GitHub standard hosted compute is
free; Blacksmith public-repo status alone does not grant unlimited free service.
Its listed base x64 price is $0.004/min with 3,000 2-vCPU-equivalent free minutes per
organization/month (checked 2026-11-20); verify larger-machine rates and allowances
in the account. Do not assume that allowance is unused or that an OSS application
is approved. Finish with before/after PR time, runner time and cost, with sample
sizes and an explicit distinction between actual billing and estimates.
