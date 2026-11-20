# CI audit: <repo> (<date>)

Mode: OBSERVE · Window: <from> → <to> · Sample: <n> runs (<m> with jobs) · Source: `.ci-audit/metrics.json`

## Answer

<One paragraph: the top bottleneck on the required path, the first intervention, expected wall-clock and runner-minute saving with confidence, and no-change findings. Separate actual acceleration from moving work to another category. If CI already meets its chosen objective with sound gates, say so.>

## Detected stack

| Item | Value | Evidence |
|---|---|---|
| CI provider(s) | | `discovery.json` |
| Languages / package manager | | |
| Monorepo graph | none / Nx / Turbo / … | |
| Test runners | | |
| Required checks | | current snapshot's `raw/required_checks.json`; source completeness/app identity |
| Trust boundaries | fork PRs? `pull_request_target`? deploy jobs? | |

## Four validation categories

Use the [validation architecture](../references/08-validation-architecture.md). Classify each command by inspected behavior, not its name; a command may cover more than one category.

| Category | Verified commands / unique failure modes | Trigger and exact revision | Gate / required completion boundary | Coverage / evidence gaps |
|---|---|---|---|---|
| 1. Code rules | | | authoritative CI; installed fast pre-push hook | |
| 2. Change correctness | relevant tests; FULL conditions | | PR acceptance | |
| 3. Integrated main | full required suite | combined queue candidate | full suite before main admission | |
| 4. Release readiness | artifact, compatibility and rollout checks | immutable artifact digest | release / promotion | |

Main policy: full suite in the required merge queue/train before main. Provider rule and workflow evidence: <links / activation blocked>. Post-push/nightly runs are supplemental.

Queue controls: <full inventory, no test-result cache replay, unique concurrency group, all entries pass>. PR-only selection fallback: <FULL>. Every obligation remains before main; list any activation blocker.

## PR feedback objective

Chosen objective: latest-revision **p50 <120 s / p95 <180 s**. Report first-push journey separately. Require wait <10 s, setup <30 s, lint/types <5 s, tests <60 s/shard and local hook <9 s. Enforced at-limit/over-budget work fails. Targets are never measured baselines.

| Metric | p50 | p95 | n / window | Start → finish / limitations |
|---|---|---|---|---|
| Latest-revision PR feedback | unknown | unknown | | exact push receipt → complete authoritative PR green for that current revision |
| First-push PR journey | unknown | unknown | | first push in PR window → first complete green for a current revision, including repair/re-push delays |
| Full integrated validation lag | unknown | unknown | | integration candidate/main event → full green, including queue and retries |
| Unvalidated / failed / cancelled / superseded | | | | counts, oldest age; keep outside successful-only latency statistics |

Timestamp provenance: <provider push events / explicitly named proxy / unavailable>. Queue, setup and reporting included: <yes / limitations>. Actual observations and target: <separate fields>. Required work that exceeds the budget safely: <keep it; next experiment or revised objective>.

## Baseline

Snapshot ID/hash manifest: <path/identity>. Enumeration and retention limits:
<complete within selected query / capped / unknown>. Cohort identity: <event,
workflow/config, runner, trust, cache, workload>. Failed fetches and missing attempt
history: <counts and effect>. Normalized dependency graph: <source/expansion proof>.

| Metric | p50 | p95 | n | Definition / limitation |
|---|---|---|---|---|
| analyzer push→green proxy | | | | approximation, not a substitute for the PR objective above |
| slowest workflow wall | | | | |
| each job: waiting | | | | created → started; separate dependency wait |
| each job: setup | | | | explicit phase markers; missing evidence unknown |
| each job: running | | | | preserve work inventory and outcomes |
| each job: teardown/unattributed | | | | do not add overlapping percentiles |
| setup share | | | | |
| shard skew | | | | |
| suspected same-run retry recovery | | | | logs distinguish flakiness, repair and intentional reruns |
| first-pass failure rate | | | | verify attempts; separate intentional reruns and repair delay |
| gap to runner-up job | | | | observational only; dependency-path recomputation determines possible saving |
| cache hit rate | unknown | | | needs logs |
| cost per merge | unknown | | | needs pricing |

Critical path: `<job A> → <job B> → <job C>` (see `critical-path.md`). Gating job: `<name>`, last in <k>/<n> runs, required: yes / no / unknown. Config changed in window: no / yes → `--since <date>`.

## Seven modules

| # | Module | Applicable | Observed bottleneck | Δ wall-clock p50/p95 (on path / below floor) | Δ runner-min/mo | Evidence tier | Correctness/security risk | Effort | Cost delta | Next proof |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Fast runners | | | | | | | | | |
| 2 | Native lint/typecheck | | | | | | | | | |
| 3 | Machines / Vitest reuse | | | | | | | | | |
| 4 | Caching / blobless clone | | | | | | | | | |
| 5 | Parallel jobs | | | | | | | | | |
| 6 | Affected tasks | | | | | | | | | |
| 7 | Local hooks | | | | | | | | | |

Combined wall-clock saving (stacked model, not the sum of rows): <value>; method: <how the path was recomputed>. Runner-minute rows add.

## Evidence identity and release eligibility

- Source identities: <PR/base/candidate/final-main SHAs and verified mapping; rerun if equivalence is unproven>.
- Workload and policy fingerprint: <commands/test IDs/coverage/graph/workflow/toolchain/build inputs; no secrets>.
- Artifact: <immutable digest, exact build source, provenance, required evidence links and expiry>.
- Eligibility: <full-green source + matching policy/artifact + category 4 checks for this stage; preflight before initial exposure, telemetry before further promotion; later main commits inherit no green>.
- Release plan: <flag states and staged exposure with telemetry thresholds/window/minimum samples/abort owner, or package/staging/consumer compatibility gates>.
- Migration/data effects: <compatibility, recovery or roll-forward proof; feature flags alone insufficient>.
- Release/deploy/publish authorization: <existing scope or required separate action; gate success alone grants none>.
- Evaluation result: <policy fingerprint, evidence observation age, requested result and exact status>. Use `--require` when consuming an evaluator result as a gate; successful report generation alone is not validation success.
- Rollout identity: <deployment ID, environment, active-stage activation time; telemetry window after activation; comparable baseline proof when relative thresholds are used>.

## Migration proof

- Selected-versus-FULL shadow comparison on identical representative revisions: <results, test identities, seeded/adversarial failures, unknowns>.
- Failure probes: <child failure, unexpected skip, cancellation/timeout, missing report/shard, wrong/stale SHA or policy/artifact mismatch all reject green/eligibility>.
- Classification of the proposal: <equivalent-work acceleration / scheduling redistribution / both, measured separately>.
- If redistributed: <work retained across PR + main + release; total runner-minutes; PR gain; full-green delay; changed detection window/risk>.

## Risks found (independent of speed)

- <false-green pattern, privileged trigger, unpinned action, missing timeout…> — file:line — severity

## PR plan (one intervention each)

| Order | PR | Module | Acceptance criteria | Owner review | Rollback |
|---|---|---|---|---|---|
| 1 | | | | | revert PR |

## No-change findings

- <module>: NOT APPLICABLE because <evidence>.

## Limits of this audit

- <missing data, low samples, approximations>
