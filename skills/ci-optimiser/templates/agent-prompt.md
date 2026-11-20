# CI optimization brief for a coding agent

Use this brief for `[OWNER/REPO]` at `[path]`. Complete authorized implementation;
an audit-only request remains read-only. Reuse authorization already given.

## Goal and non-negotiable rules

Make PR feedback p50 <120 seconds and p95 <180 seconds, including waiting, setup,
work and final required-check reporting. Run the **full suite on the combined
merge-queue candidate before main**. Selection narrows PR feedback only; bypass
test-result cache reads in the queue. Post-push/nightly work is supplemental.

Never hide failure through retries, wider timeouts, skips, quarantine, reduced
coverage or ignored exits. Diagnose the first failure and fix it. Keep every failed
benchmark trial and order seed visible. A replacement must catch everything the
old tool did or retain a specific measured compatibility check.

## Work in this order

1. Read repository policy and actual commands, CI topology, required checks and
   provider rules. Keep untrusted code away from privileged tokens and caches.
2. Pull the last 150 runs and all jobs/attempts. Report fewer available runs,
   pagination/access gaps and retention. Split **every job** into waiting, setup,
   running and residual time at p50/p95; show n, failures, cancellations and reruns.
   Unknown timing is not zero. Fix the largest measured bucket on the required path.
3. Classify code rules, change correctness, full integration and release readiness.
   Verify an active queue rule plus `merge_group` workflow on GitHub. Preserve the
   required aggregate; a skipped/cancelled/missing child cannot pass it. Give queue
   runs unique concurrency groups; cancel superseded PR runs only.
4. Apply stack-appropriate defaults: every repository job on Blacksmith after app
   activation; waiting <10 s. Setup <30 s, >95% exact PR hits from trusted main,
   exact-compatible `node_modules` cache, frozen cold installs, full-history blobless
   checkout. Record a measured reason or activation blocker for any exception.
5. Compare old/new side by side before each tool switch. Prefer Oxlint (<5 s),
   `bun check --no-script` (<5 s; TS7 fallback), Oxfmt, Bun installs/scripts and tsrs
   locally. Compare rules, files, diagnostics, compiler options, declarations,
   formatter/plugin coverage and lifecycle behavior. Remove old tools after parity.
6. Parallelize independent lint/types/build/tests. Separate migrations and unrelated
   apps into their own required jobs. Benchmark 8-core test machines before adding
   shards; balance by measured durations, <60 s/shard, worker count matched to CPU
   and memory, a DB per shard plus worker data isolation.
7. Prove file cleanup and leak resistance before Vitest `isolate: false`. Execute
   cleanup and document it in AGENTS.md; keep seeds and test IDs/count/coverage.
8. Use Nx/Turbo affected only on PRs with a trustworthy graph and FULL fallback.
   Deliberately hide a real breaking dependency change; execute selected and full
   suites and require the guard to turn red. Queue execution remains unfiltered.
9. Install an actual <9-second pre-push hook for changed-file format/lint and
   affected-project/dependent typechecking. Preserve existing hooks, use safe argv,
   verify execution and failure propagation. CI remains authoritative.
10. Enforce each job/stage budget without masking underlying failures. Keep rolling
    percentile checks, exact cache-hit ratios and per-command limits distinct.
    Unknown evidence cannot certify a target; report a live verifier's own pending
    completion honestly and verify it after completion.
11. Open one small PR per independently reversible intervention with **real** linked
    before/after runs, source/tool/config identity, n, workload/coverage parity,
    misses, cost and rollback. Never infer p95 from one run or call deferred work
    eliminated. Merge only through the full-suite queue when authorized.

Finish all possible work before returning one account/app/secret/admin/spend list
with expected monthly costs, rates, allowances and assumptions. Finish with total
CI time, aggregate runner use and cost before/after; separate estimates from billing.
Do not invent a PAT requirement or upgrade plan when the existing account suffices.

Read the [strict contract](../references/14-two-minute-ci.md), [tool migration](../references/15-toolchain-migration.md),
[audit field guide](../references/12-field-guide.md) and [evidence formats](../references/11-evidence-formats.md).
Helpers provide scoped evidence, not automatic source authentication, queue enforcement
or deployment authorization. Keep exact revision/artifact provenance for releases.
