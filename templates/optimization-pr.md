## What

<One reversible intervention, module(s) and validation categories affected. Files touched and resulting behavior.>

## Why

<Bottleneck it targets, with the metric and source: e.g. "test job is 62% of the critical path, p50 14m12s over 120 runs, .ci-audit/metrics.json 2026-10-01..2026-10-31".>

## Validation contract

| Category | Commands / evidence verified | Before → after boundary | Retained required gate |
|---|---|---|---|
| Code rules | | | |
| Change correctness | relevant tests; high-risk/unknown FULL fallback | | |
| Integrated main | full required suite | combined queue candidate before main | |
| Release readiness | exact artifact and compatibility/rollout checks | | |

Integration policy: required full-suite merge queue/train before main. Active provider rule and workflow evidence: <links>. Queue selection/cache bypass and unique concurrency group: <proof>. Post-push verification does not substitute.

PR objective: latest-revision p50 <120 s / p95 <180 s. Observed baseline: <value / unknown>. Per-job wait <10 s, setup <30 s, lint/types <5 s and test work <60 s; local hook <9 s. At-limit or exceeded budgets fail; unfinished work never turns green.

## Evidence

| Metric | Baseline | Candidate | Delta | n | Definition |
|---|---|---|---|---|---|
| latest-revision PR feedback p50/p95 | | | | | exact push receipt → complete current-revision PR gate |
| first-push PR journey p50/p95 | | | | | includes repair/re-push delays |
| full integrated validation lag p50/p95 | | | | | candidate/main event → full green |
| total runner-minutes across PR + main + release | | | | | |

Workload state: same / reduced / increased / changed / unknown (evidence: <job and test identities>). Gating job before → after: `<a>` → `<b>`. Product code touched: no / yes (then the delta is not CI-only).

Experiment record: `experiments/<id>.json`. Cold and warm cache measured separately: yes / no.

Claim: <equivalent-work acceleration / scheduling redistribution / both>. If work moved later: <which obligations, retained gates, detection delay and accepted risk; do not count deferred work as eliminated>. Failed/cancelled/superseded/unvalidated cohorts: <counts/ages>. Timestamp source/proxy and sample limitations: <details>.

Identity record: <PR/base/queue-candidate/final-main/build SHAs and verified mapping or rerun>. Artifact digest: <digest / not applicable>. Validation/workflow/toolchain/input policy fingerprint: <digest>. Full-green and readiness evidence/expiry: <links>. A later SHA, changed policy or rebuilt artifact requires applicable fresh proof.

## Correctness gates

- [ ] Required check names unchanged (or branch rules updated with owner approval: <link>)
- [ ] Forced failure in each affected job turns the required check red (probe commits: <shas>)
- [ ] Every original obligation has a retained gate at the approved boundary; same-workload comparisons preserve test identity/count/coverage (<evidence>)
- [ ] Selection/placement changes shadowed against FULL on the same representative revisions; seeded/adversarial failures caught
- [ ] No new `continue-on-error`, `|| true`, or always-skip `if:`
- [ ] Cold-cache run green
- [ ] Selector changes: high-risk/unknown cases return FULL before integration; transitive/deleted/renamed/config inputs covered
- [ ] Isolation changes: ≥3 shuffled seeds green
- [ ] Unexpected skip/cancellation/timeout/missing report or shard cannot satisfy a required gate
- [ ] Wrong/stale revision, artifact or policy evidence cannot grant release eligibility
- [ ] Full merge-queue suite executes without affected filters/test-result cache reads; main cannot bypass it
- [ ] No new retries, wider timeouts, quarantine or skipped tests; initial failures remain visible
- [ ] New tool missing-rule/file/diagnostic ledger is empty or specific required fallbacks remain
- [ ] Installed pre-push hook and actual budget failures verified where changed

## Release readiness impact

<Not applicable / flags and enabled-disabled compatibility / migration and data safety / immutable artifact staging / package-install and consumer matrix>. Progressive exposure: <thresholds, window, minimum samples, abort owner; missing telemetry pauses>. Feature flags do not replace migration compatibility. Passing this gate does not authorize deployment, exposure or publication.

## Security

- Permissions changed: none / <diff> (security-owner review: <name>)
- Cache trust boundary changed: none / <description>
- New dependency or vendor: none / <name, maintainer, license, install scripts, cost> (project-owner approval: <link>)

## Rollback

Revert this PR and restore the previous gate/selection policy. Thresholds: <agreed feedback/full-green/flake limits and sample window>. Reject any false green, coverage gap, wrong artifact eligibility or validation starvation immediately. Repair/revalidate affected evidence; do not promote an artifact based on a superseded policy.

## Tool and budget evidence

| Old tool/check | Candidate | Before/after run links | Diagnostics/rules/files missing | Retained fallback / reason |
|---|---|---|---|---|
| | | | | |

Per-job wait/setup/work p50/p95 and sample counts: <table>. Exact-cache hits:
<numerator / eligible restores, misses and unknowns>. Hidden-change negative control:
<real source diff, selected/full exits, guard result>. Budget negative control:
<command/phase at limit remains nonzero>.

## One activation and cost list

| Account/app/secret/admin/spend blocker | Prepared change | Expected monthly cost | Rate, volume, allowance and uncertainty |
|---|---|---|---|
| none / specific item | | | |

Final before/after: PR p50/p95 <values and n>; total runner seconds/minutes <values>;
monthly compute/storage <actual / estimate / unknown>. Include queue and trusted-main
cache runs. Never price the critical-path wall time as if it were all runner work.
