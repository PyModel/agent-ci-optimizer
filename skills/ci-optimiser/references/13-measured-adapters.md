# Measured adapters

Use these read-only or offline tools only as needed. Keep raw evidence local and
store examples and outputs outside target workflow checkouts. See the [field
guide](12-field-guide.md) and [evidence formats](11-evidence-formats.md). A helper's
diagnostic success is not an authorization to change gates or release.

## Evidence chain

- Keep provider retrieval, policy review, artifact attestation verification,
  and gate evaluation distinct. A current successful job is not proof that a
  particular historical policy was in effect or that a particular binary ran.
- Pin repo identity, exact commit, candidate event, workflow/job identity,
  check app, attempt, and observation time. Do not revive an old success after
  a newer failure or an unfinished rerun. Pagination gaps are not absence.
- Verify artifact bytes against the reviewed trusted signer/workflow and source
  identity. A checksum proves byte equality, not a trusted producer. Do not
  rebuild from a mutable ref to substitute for a verified artifact.
- Shadow affected-test changes with a complete, comparable FULL control.
  Record both suites' exact inventories and results on the same revision and
  compatible execution context. A clean observation does not alone prove
  selection safety; repeat across risk classes and seeded cases.

## Runner lifecycle and telemetry

- Distinguish a runner registered for one job from a clean machine image. Keep
  untrusted fork work, trusted PRs and privileged release jobs in isolated pools.
  Verify image provenance, version updates, network exposure, per-job teardown,
  cancellation cleanup, and externally retained logs before ephemeral teardown.
- Measure queue-to-ready and dependency-ready-to-start separately. Measure CPU
  time, peak resident memory, I/O, transfer and thermal/memory pressure. Model
  VM startup, host reserve, and exclusive simulator or emulator access.
- Treat free hosted allowances, self-hosted hardware, paid runner cost, and
  incremental compute as different quantities. Pair the model with actual
  billing or measured resource use; list assumed rates, allowances, idle
  time, hardware amortization, energy, storage and operator time separately.
- Use a scheduler only when observed constraints justify it. For a small
  macOS/Linux host, benchmark isolated native and guest workloads and their
  overlap before adding a pool manager. See [GitHub self-hosted runner
  guidance](https://docs.github.com/en/actions/reference/runners/self-hosted-runners).
- Optional [OpenTelemetry CI/CD conventions](https://opentelemetry.io/docs/specs/semconv/cicd/)
  can guide import/export. Pin their schema revision; do not invent missing
  span measurements or require a new telemetry service.

## Case-based evaluation

For changes to the skill itself, use the separate [behavior protocol](../evals/README.md).
Script correctness and packaging checks are not model-behavior evidence.

## Commands and evidence boundaries

Run from the skill root; pass reviewed file paths. An exit code of zero applies
only to that helper's documented result. Use `--help` before collecting anything.

| Helper | Input and claim boundary |
|---|---|
| [GitHub check collector](../scripts/collect_gh_evidence.py) | `--repo OWNER/REPO --sha SHA --expect FILE --out FILE`, with authenticated read-only `gh` on GitHub.com. The expected identities are reviewed input; check conclusions come from GitHub. Emits `ci-github-check-evidence/1`, not an integrated-main policy or release-pass assertion. |
| [Artifact attestation verifier](../scripts/verify_gh_attestation.py) | Local bytes, absolute reviewed GitHub CLI and its SHA-256, exact source repo/source SHA and signer workflow/signer SHA. Uses the pinned executable in a trusted POSIX environment; offline fixtures do not establish live availability. Reports provenance, not test pass or deployment approval. |
| [GitHub cache inventory](../scripts/collect_gh_cache.py) | `--repo OWNER/REPO` (GitHub.com, authenticated read-only `gh`); redirect stdout to a private file. Exit 3 means incomplete. Hashes keys/versions and preserves ref, size and timestamps. Not transactional; cannot infer hits or eviction causes. |
| [Cache economics](../scripts/analyze_cache.py) | Pass a `ci-cache-events/1` diagnostic file or `/2` typed layer/context evidence with separate restore/save outcomes. Unknown inputs stay unknown; savings are runner seconds, not a critical-path gain or a bill. |
| [Shard planner](../scripts/plan_shards.py) | `--inventory FILE --timings FILE --shards N`; normalized runner-ID JSON and reviewed coverage/serial contracts. Proposes inventory-preserving static assignments; no native JUnit import, runner dispatch, or coverage certification. Verify exact test execution and coverage separately. |
| [Selector shadow evaluator](../scripts/evaluate_shadow.py) | One `ci-selector-shadow/1` file with exact selected/FULL inventories and results on the same revision and context. Exit 0 means one clean controlled comparison, never permission to remove full validation. |
| [Capacity and cost model](capacity-model.md) | Explicit finite pools and exclusive locks with optional supplied rates. A deterministic modeled estimate, not measured queue, total ownership cost, or an optimal schedule. |
| [GitLab collector](../scripts/collect_gitlab.py) | `--project GROUP/PROJECT --pipeline ID --out FILE`. Use a read-only `GITLAB_TOKEN` where required; `--server` must be an operator-trusted HTTPS instance. Preserves native queue and retried/child data. Snapshot is non-atomic and incomplete access stays explicit; do not interpret it as merge eligibility or queue-cause evidence. |

### GitHub check expectations example

```json
{
  "schema": "ci-github-expectations/1",
  "repository_id": 100,
  "workflow_id": 200,
  "event": "push",
  "max_check_age_s": 86400,
  "jobs": [{"name": "ci", "app_id": 123}]
}
```

IDs, event, and freshness here are illustrative, not defaults. A consumer of
multiple evidence sources must bind exact candidate, workflow/policy scope and
artifact digest, authenticate provenance, and revalidate freshness before an
eligible decision. Do not concatenate these JSON files and mark release verified.

### Normalized offline input examples

Example inventory and timing files (use separate JSON documents):

```json
{"schema":"ci-test-inventory/1","tests":[{"id":"orders.test","serial_group":"database","covers":["orders"]}],"required_coverage":["orders"]}
```

```json
{"schema":"ci-test-timings/1","tests":[{"id":"orders.test","duration_s":12.5}]}
```

Use duration aggregates from comparable runs; do not double count retries.
Unknown timings may affect the scheduling estimate but never justify dropping tests.

Example cache evidence with explicit comparison and costs:

```json
{"schema":"ci-cache-events/1","events":[{"id":"event-1","cache":"dependencies","cohort":"linux/toolchain-a/workload-a","hit":true,"outcome":"exact_hit","restore_s":3,"save_s":0,"work_s":2,"uncached_work_s":20,"baseline_comparable":true,"restored_bytes":1000,"saved_bytes":0}]}
```

Record provider-derived events separately from inventory snapshots. In a mixed
cohort, an exact hit, fallback hit, miss, restore failure, save failure,
write-disabled state, and unknown are distinct outcomes. A measured sample
does not prove that the difference is on the required path.

`negative_savings_event_count`, `negative_savings_event_ids` and the
`measured_negative_cache_events` warning expose measured losses even when a
positive event offsets them or incomplete costs leave total savings unknown.
These are observation-level investigation signals, not proof of a population
regression or permission to remove a cache. The aggregate recommendation remains
conservative and missing costs remain unknown.

### Version 2 layered cache evidence

Use `ci-cache-events/2` for new adapters. `/1` remains a diagnostic format with
the original supplied `baseline_comparable` assertion; do not silently upgrade
it to verified evidence. `/2` emits `ci-cache-analysis/2`, rejects mixed legacy
`hit`, `outcome` or `baseline_comparable` fields, and keeps restore/save lifecycle
facts independent. Neither version is an acceptance gate.

Each event needs bounded, nonblank `id`, `cache` and `cohort` strings. Optional
`restored_bytes` and `saved_bytes` are nonnegative integers or null. The lifecycle
fields default to `unknown`:

| Field | Values |
|---|---|
| `restore_outcome` | `exact_hit`, `fallback_hit`, `miss`, `restore_failure`, `disabled`, `unknown` |
| `save_outcome` | `saved`, `not_needed`, `write_disabled`, `save_failure`, `unknown` |

A fallback restore followed by a failed save therefore preserves both facts.
Version 2 derives a generic hit from an exact/fallback restore, a miss from
`miss`, and leaves other restore results unknown. Save status does not establish
a restore hit. Group counts retain both lifecycle fields.

`comparison`, when supplied, has exactly four fields: `layer_under_test`,
`baseline`, `candidate`, and `review_reference`. The layer must be one of
`runner_snapshot`, `toolchain`, `dependencies`, `build_outputs`, `test_results`,
or `action_archive`. The review reference identifies reviewed evidence that any
configuration difference changes only this layer. Each baseline/candidate
context accepts these fields; missing or null evidence remains unresolved:

| Context field | Contract |
|---|---|
| `repository`, `workflow`, `run_id`, `job_id` | Bounded immutable observation identities; workflow is the reviewed workflow path/identity on the pinned revision. |
| `revision` | Full lowercase 40- or 64-character Git object ID. |
| `attempt` | Integer from 1 through 1000; do not pool first attempts with retries. |
| `config_digest` | SHA-256 of the reviewed configuration manifest, recorded for each arm. |
| `action_digest`, `toolchain_digest`, `runner_image_digest`, `workload_digest` | Lowercase SHA-256 fingerprints of reviewed immutable manifests, identical between arms. A mutable image or action tag is insufficient. |
| `states` | Exact six-layer map described below. |

Every layer row has exactly `state`, `identity`, and `evidence`. State is `cold`,
`warm`, `disabled` or `unknown`; identity and evidence are trimmed printable
strings of 1–1024 characters or null. Use snapshot IDs or reviewed manifests;
do not hash large cache trees indiscriminately or delete shared caches. Even a
`cold` or `disabled` label needs identity and evidence to be resolved. For example:

```json
{"state":"warm","identity":"snapshot-2026-11-20-a","evidence":"reviewed-manifest/run-42/job-7/runner-snapshot"}
```

Both contexts must identify separate observations with the same repository,
revision, workflow, attempt, action/toolchain/image/workload fingerprints, and
the same state and identity for every layer outside the layer under test.
Evidence references may differ between observations. The selected layer must
actually differ by state or identity. For archive on/off, preserve the same
warm runner snapshot and other cache layers; cold disk versus warm disk cannot
isolate the archive's effect. Changed known context is `incompatible`; absent
evidence is `unknown`. Only a complete matched pair is
`comparable_supplied_evidence`, always with `authenticated: false`. The helper
validates structure and equality; it never reads a reference, authenticates a
producer, or establishes causality from a digest.

Choose one explicit `timing_model` and a `timing_scope` evidence reference for
the matching complete measurement boundary, including post-job saves:

| Model | Required finite nonnegative times | Net runner seconds |
|---|---|---|
| `sequential_components` | `restore_s`, `save_s`, `work_s`, `uncached_work_s` | `uncached_work_s - work_s - restore_s - save_s`; assert the components do not overlap. |
| `elapsed_total` | `elapsed_s`, `uncached_elapsed_s` | `uncached_elapsed_s - elapsed_s`; optional component diagnostics are retained but never added to the elapsed total. |
| `unknown` or missing | None | Unknown economics. |

“Uncached” here means the baseline without the selected cache layer, not a
machine with every cache emptied. A missing scope, missing required time, or
unresolved comparison leaves `net_saved_s` null. Use explicit zero for an
observed zero cost. Whole-path elapsed time is necessary when restore, work or
saving overlap. Preserving diagnostic component costs does not make them
additive.

This valid incomplete example deliberately produces unknown economics, even
though it supplies 35- and 67-second elapsed values. These are synthetic schema
demonstration values, not a reproduced Helm/Zode experiment:

```json
{"schema":"ci-cache-events/2","events":[{"id":"archive-on-1","cache":"go","cohort":"warm-snapshot-a","restore_outcome":"fallback_hit","save_outcome":"save_failure","comparison":{"layer_under_test":"action_archive","baseline":{},"candidate":{},"review_reference":null},"timing_model":"elapsed_total","timing_scope":"review/setup-through-post-save","elapsed_s":67,"uncached_elapsed_s":35}]}
```

Cache reuse and measured economics never establish fresh required test
execution. Keep test-result reuse separate from dependency/build reuse and run
the full suite freshly on the combined merge-queue candidate before main.

### Exact-hit target and eligible PR population

`observed_hit_rate` remains the generic diagnostic: known hits divided by known
hits plus known misses, excluding unknowns. In `/2`, both exact and fallback
restores count as generic hits. This is not the strict `>95%` exact-hit target.

For target assessment, each `/2` event explicitly declares
`eligible_pr_restore: true`, `false`, or `null` (missing means unknown). Do not
infer eligibility from a saved archive, a write-disabled policy, a hit, or a
miss. Keep eligible lockfile-change misses, errors and unresolved attempts in
scope. Document justified exclusions such as save-only observations in the
population evidence; do not exclude misses to improve the rate.

Supply a top-level `populations` array. Each row has exactly `cache`, `cohort`,
`eligible_count`, `complete`, `scope` and `evidence`, with a unique cache/cohort
pair. Count is the independently collected full eligible PR population (an
integer from 0 through 100000 or null). `complete` is boolean or null; scope and
evidence are bounded references or null. Define repository, PR events, time
window, attempts and eligibility rules in the reviewed scope/evidence. A caller
assertion does not authenticate provider completeness.

```json
{
  "schema":"ci-cache-events/2",
  "populations":[{"cache":"deps","cohort":"PR-window-a","eligible_count":3,"complete":true,"scope":"repo-a/PRs/attempt-1/window-a","evidence":"review/all-eligible-restores"}],
  "events":[
    {"id":"exact","cache":"deps","cohort":"PR-window-a","eligible_pr_restore":true,"restore_outcome":"exact_hit"},
    {"id":"fallback","cache":"deps","cohort":"PR-window-a","eligible_pr_restore":true,"restore_outcome":"fallback_hit"},
    {"id":"unresolved","cache":"deps","cohort":"PR-window-a","eligible_pr_restore":true,"restore_outcome":"unknown"}
  ]
}
```

Here the generic hit rate is 100%, while `exact_hit_target.known_exact_fraction`
is `1/3` and target status is `unknown`. The target reports eligible exact,
fallback, miss, restore-failure, disabled and unknown outcomes separately, as
well as unknown eligibility and observed ineligible events. Missing events stay
visible as a population-count mismatch. A population with no supplied events
still gets an output group; it cannot silently disappear or imply zero cost.

Target status is `met` or `not_met` only for a complete supplied nonempty
population with matching counts, known scope/evidence, and no unresolved
eligibility or outcomes. An eligible-but-disabled restore is unresolved for this
purpose. Exactly 95% is `not_met`; comparison uses integer counts without
rounding. Missing population evidence or legacy `/1` eligibility is `unassessed`;
partial or contradictory evidence is `unknown`. No eligible population is also
`unassessed`, not a vacuous pass. A known exact fraction above 95% does not make
incomplete evidence complete.

`met` describes this supplied population only, not an authenticated policy
certificate, future guarantee, or successful workflow. Restore failures remain
counted and job failures remain failures. High exact-hit rates never override
negative measured cache economics or authorize test-result reuse.

### Remaining integration work

Provider fixtures cover the contracts of the new collectors; they are not live
provider verification runs. Native JUnit parsing, OpenTelemetry import/export,
calibration against real runner/host pressure and total ownership data, live
signature verification, and measured agent behavior remain separate work.

## Strict budget helpers

`run_budget.py --seconds 60 --label tests -- COMMAND` executes one argv with a
monotonic exclusive time bound, no retries, preserved failures and bounded POSIX
process-group cleanup. Its Windows fallback controls only the direct child. It
measures one command, not queue/setup or p50/p95.

`check_job_budgets.py --jobs FILE --policy FILE --attempt 1 --run-id ID
--head-sha SHA [--current-job NAME]` verifies complete GitHub job pages against an
exact job/step policy and expected run/head identity. Its `ci-job-budgets/1` policy
has exact job names, waiting_s/setup_s/running_s/runner_s limits, the last setup
step name and an explicit work-step inventory. Unknown or replayed evidence fails.
Input reads are bounded. This offline verifier does not authenticate caller data.

Bind SHA to the API workflow/job head_sha; for PR events that may differ from the
checkout merge SHA. Obtain fresh provider data with read-only permissions. The
explicit current verifier job may report pass_with_pending: only its observed
waiting/setup pass, while its future running/runner duration remains pending.
Guard its command and audit its completed duration later. Never call that a fully
completed workflow-budget certificate or a provider queue-rule verification.
