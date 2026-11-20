# Four validation categories

Apply the [strict performance contract](14-two-minute-ci.md) first. Use this reference when reorganizing validation across PR, integrated-main and release workflows. Adapt it to the detected provider and stack; categories describe evidence, not mandatory job names or new tooling. Keep the [security invariants](03-security-correctness.md) and [measurement rules](04-measurement-benchmarking.md).

## Contents

- [Questions and evidence](#questions-and-evidence)
- [PR feedback budget](#pr-feedback-budget)
- [Placement algorithm](#placement-algorithm)
- [Integrated-main policy](#integrated-main-policy)
- [Exact evidence and artifact eligibility](#exact-evidence-and-artifact-eligibility)
- [Release readiness](#release-readiness)
- [Migration and measurement](#migration-and-measurement)
- [Worked examples](#worked-examples)
- [Sources](#sources)

## Questions and evidence

| Category | Question | Typical evidence, only after inspecting the command | Required boundary |
|---|---|---|---|
| 1. Code rules | Does the code meet the repository's rules? | Formatting, lint, static analysis, type and policy checks | Authoritative CI for the change; optional local feedback |
| 2. Change correctness | Does this change behave correctly? | Relevant unit, contract, integration and regression tests; affected builds | PR acceptance, with FULL fallback for risk or uncertainty |
| 3. Integrated main | Does the complete integrated revision work? | Full required suite and compatibility/build checks on a merge candidate or exact main commit | Full required suite on the combined queue candidate before main admission; always before release eligibility |
| 4. Release readiness | Can this exact artifact be released and operated safely? | Packaging/provenance, staging, migration compatibility, feature-gate and progressive-rollout evidence | Release/promotion gate; deployment still needs its own authorization |

Inspect script bodies, configuration, selected files, flags, exit propagation and emitted artifacts. A command named `lint` may also build or test; `check` may do neither. Record unique failure modes and seeded failures it catches. A command can supply several categories; split it only when dependencies and enforcement remain explicit. Unknown behavior stays unclassified and cannot justify deleting a gate.

## PR feedback budget

Target latest-revision PR feedback p50 <120 seconds and p95 <180 seconds. Record the owner's chosen budget and measured baseline separately; never report the targets as observed results. Measure p50/p95 end to end, including queue, setup, all required PR work, retries and aggregate reporting:

| Metric | Start | Finish and interpretation |
|---|---|---|
| Latest-revision PR feedback | Provider receipt of the exact pushed revision | Authoritative PR gate reports a trustworthy success or genuine failure for the current revision; report outcomes separately |
| First-push PR journey | First pushed revision in the defined PR observation window | First current revision achieves the complete PR gate; includes repair/re-push delays and shows the user's total wait |

Latest-revision feedback measures time to a trustworthy result; time to green also includes repair/re-push delay. Cancelled, superseded, unfinished and budget-aborted runs retain explicit censored/breach status. Existing helper success-latency proxies do not implement this broader result metric. State which metric each observation covers and report both. Use actual push/event timestamps, not author/commit dates. If unavailable, mark the metric unknown or name the run-created proxy explicitly. Keep failed, cancelled, superseded and still-unvalidated observations visible with counts/ages; successful-only percentiles do not describe the whole loop. Record sample size, cohort, config era and cache state; small-sample p95 is preliminary.

A timeout bounds a stuck process; it is not a feedback budget. Enforce measured per-run stage/job budgets with a nonpassing result at or above the limit. Diagnose breaches and change capacity or scheduling; do not silently revise the objective. It never authorizes skipping tests, downgrading checks, masking failure or declaring unfinished work green. High-risk changes may legitimately require a longer PR path.

## Placement algorithm

1. Inventory the four categories, current commands, unique failure modes, required check identities and trust boundaries. Mark absent evidence unknown; retain the current protection until replacement is proven.
2. Verify and configure the required full-suite merge queue/train. Document the evidence preceding PR feedback, queue admission, artifact eligibility and release; report unavailable enforcement as a blocker.
3. Map the diff through verified dependencies and change-to-check contracts. Run relevant category 2 tests on PRs; treat security/authentication, payments, schema/migrations, shared contracts, generated inputs, CI/selector policy and other declared high-risk changes conservatively. High risk or unknown base/graph/coverage/confidence forces FULL before integration.
4. Retain category 1 rules and every uniquely required test at its approved boundary. Include transitive dependents and non-code inputs. Do not infer relevance from filenames alone or skip integration tests solely because they are slow.
5. Optimize the measured required path using the seven modules. Install a measured <9-second pre-push hook for applicable checks; CI remains authoritative and hook bypass never counts as proof.
6. Validate selected-versus-full equivalence and gate failures in shadow mode before migrating. If safe placement cannot meet the budget, keep the protection and report the trade-off.
7. Require full integrated evidence plus category 4 evidence for the exact release artifact. A later lane cannot retroactively protect an earlier boundary.

## Integrated-main policy

Require a merge queue/train to validate the combined candidate against the current
base and queued predecessors. Admit it only after the full required suite and stable
aggregate pass. On GitHub subscribe to `merge_group`, disable affected selection and
test-result cache reads on that event, and verify the active provider rule requires
all queue entries to pass. Post-push or nightly verification is supplemental only.

Keep queue/main concurrency groups unique; cancel superseded PR runs only. Missing
queue access or unsupported provider semantics is a reported blocker, not permission
to admit untested main. Preserve checks while preparing activation.

The evaluator still reads historical `rolling` and `all_pr` policy records for
compatibility and diagnosis. Those modes do not satisfy this skill's strict contract.
Their output is not proof that a queue rule exists or that it controlled admission.
See [queue and cancellation](14-two-minute-ci.md#queue-and-cancellation).

## Exact evidence and artifact eligibility

Bind each gate result to immutable evidence. Retain this minimal record in the provider's appropriate durable store:

| Field | Meaning |
|---|---|
| Source identity | PR head, base, tested merge-candidate SHA, final main SHA and their verified relationship; repository and event |
| Workload identity | Commands, selected/full test identities, expected shard inventory, coverage, dependency graph and relevant configuration |
| Policy fingerprint | Digest of validation/selection policy, workflow and trusted dependencies/toolchain versions, relevant build inputs/environment; no secrets |
| Execution identity | Run/attempt, runner/platform, times, conclusions, evidence links and verified PR-only selection omissions; no omitted queue obligations |
| Artifact identity | Content digest, build source SHA, builder/workflow provenance, toolchain/config fingerprint and release target |
| Eligibility | Required categories satisfied, completion time/expiry, accepted policy and unresolved failures |

Keep the queue candidate SHA distinct from the final main SHA: squash/rebase/provider merge behavior can change identity. Record the provider's mapping and prove that evidence applies to the final source/build inputs, or rerun the necessary validation. A matching source tree alone does not prove equivalence when versioning, generated files or builds depend on commit identity, ref or environment. Never attach a prior green to a different SHA by assumption.

Release only an immutable artifact whose source and policy fingerprint match accepted full-green evidence and whose category 4 checks for the current stage pass. Preflight precedes initial exposure; observed rollout telemetry gates subsequent promotion, not the already completed preflight. Record the exact digest when promoting an existing artifact. A rebuild creates a new artifact and must establish its own provenance and required verification; a floating `main` ref or image tag is insufficient. Changed policy/input fingerprints invalidate reuse until evaluated. Attestations can establish provenance but do not themselves prove tests passed ([GitHub overview](https://docs.github.com/en/actions/concepts/security/artifact-attestations), checked 2026-11-17).

## Release readiness

For deployable services, define safe default flag states, test enabled/disabled paths and compatibility, then propose staged exposure with explicit health/error/latency/business thresholds, observation windows, minimum traffic/sample counts and an abort owner. Missing/inconclusive telemetry pauses progression; lack of observed errors is not evidence when there is no traffic. Pin the tested artifact throughout the rollout. A passing readiness gate grants eligibility, not permission to deploy or expose a feature.

Feature flags do not undo schema changes, backfills, irreversible side effects or incompatible APIs. Require appropriate expand/contract and migration compatibility checks, data recovery/roll-forward plans and staging validation. For libraries, CLIs, offline software or low-traffic systems, use release-candidate packaging/install tests, supported-platform/consumer compatibility, staging or a supervised validation window instead of inventing a traffic canary. Keep irreversible publishing separately authorized.

Choose the existing platform's rollout mechanism; do not require Kubernetes or a new vendor. For a concrete optional example, Argo Rollouts supports analysis outcomes that continue, abort or pause a rollout ([analysis guide](https://argo-rollouts.readthedocs.io/en/stable/features/analysis/), checked 2026-11-17). Define equivalent behavior for other platforms.

## Migration and measurement

1. Capture the original rule/test/release contract and map every obligation to its proposed category, executor and enforcement boundary. Keep check names stable or migrate protection rules explicitly with owner authorization.
2. Shadow selected and FULL work on the same representative revisions. Compare identities, outcomes, coverage and seeded violations, including transitive changes, renames/deletions, lockfiles, generated code, contracts and selector errors. A false negative or unknown mapping rejects selection and restores FULL.
3. Probe every required aggregate with child failure, unexpected skip, cancellation, timeout, missing shard/report and stale/wrong-SHA evidence. Prove release eligibility rejects an unvalidated commit and mismatched artifact/policy fingerprint.
4. Keep unchanged-workload acceleration separate from scheduling redistribution. For actual speed claims, compare equivalent commands/tests/runner classes/cache cohorts. For redistribution, report work moved, total runner-minutes across PR + main + release, PR latency, time to full integrated green, detection delay and changed residual risk; do not call deferred work eliminated or an equivalent-work speed-up.
5. Adopt one reversible intervention at a time. Monitor both feedback and full-validation progress; roll back on false greens, stale evidence, starvation, coverage gaps or violated risk policy. Do not reduce coverage to make a dashboard target green.

## Worked examples

**Polyglot service (Go API + Next.js UI).** Inspect actual scripts first. Category 1 may include formatting/static checks and route-aware type validation. Category 2 runs relevant Go/UI tests plus changed API contracts; payment/auth/schema changes force FULL. Target latest-revision p50 <120 seconds and p95 <180 seconds, with observed values initially unknown. Category 3 validates the integrated backend/frontend and required platform/database matrix through the existing merge queue. If a queue is unavailable, prepare the configuration and report activation as blocked; do not merge. Category 4 validates the immutable application artifacts, schema compatibility and a telemetry-gated staged release; a feature flag does not replace migration checks.

**Small library.** If the full required suite already meets the chosen PR objective, keep it on every PR. Run formatting/types/API checks and full tests together if that is cheapest; no graph, shards or dependency cache is required merely to fill four categories. Still require full pre-main merge-queue verification. Category 4 verifies the built package, declaration/API compatibility, installation and supported consumers before an authorized publication. Use release-candidate consumers or staging instead of traffic percentages; preserve the exact package digest.

## Sources

Architecture, budgets and policy choices above are proposed design rules, not provider guarantees or measured results. Provider facts were checked 2026-11-17: [GitHub merge queues](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue), [artifact attestations](https://docs.github.com/en/actions/concepts/security/artifact-attestations), [Argo progressive analysis](https://argo-rollouts.readthedocs.io/en/stable/features/analysis/). See [official sources](06-official-sources.md) for the maintained source register.
