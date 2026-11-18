# Evidence formats and executable helpers

All helpers require Python 3.10+ and the standard library. They do not execute
repository configuration to discover commands. Read `--help`, review input sources,
and keep evidence outside version control. A data file can assert false facts;
format validation is not provider authentication, signature verification or policy
approval. Trust those boundaries only after independent verification.

## Contents

- [Collection snapshots](#collection-snapshots)
- [Analysis and cohort identity](#analysis-and-cohort-identity)
- [Dependency graphs](#dependency-graphs)
- [Benchmark context and comparability](#benchmark-context-and-comparability)
- [Workflow semantic input](#workflow-semantic-input)
- [Four-category policy](#four-category-policy)
- [Validation evidence](#validation-evidence)
- [Result consumption](#result-consumption)
- [Report transport](#report-transport)

## Collection snapshots

```sh
python3 scripts/collect_gh.py --repo OWNER/REPO --out .ci-audit \
  --limit 150 --event pull_request --target-branch main \
  --created 2026-11-01..2026-11-17 --include-incomplete --incremental
```

`--branch` selects run **head history**; `--target-branch` selects the protected
merge target. Without the latter, repository default-branch metadata resolves the
target; inaccessible metadata stays unknown. Use `--workflow` to narrow the query.
Partition large history windows explicitly; a capped sample is not full history.

| Path under the output directory | Meaning |
|---|---|
| `current.json` | `ci-snapshot-pointer/1`, atomically selects snapshot ID/path/repository |
| `snapshots/ID/raw/runs.json` | Selected retained workflow runs, including failures |
| `snapshots/ID/raw/jobs/RUN.json` | Successfully fetched paginated jobs; absent on fetch failure |
| `snapshots/ID/raw/attempts/RUN.json` | Available run-attempt metadata; completeness is separate |
| `snapshots/ID/raw/required_checks.json` | Positive required-check/workflow identities plus source completeness |
| `snapshots/ID/raw/collect_meta.json` | Filters, timestamps, enumeration scope, errors, freshness and transport version |
| `snapshots/ID/raw/manifest.json` | `ci-snapshot-manifest/1`, payload hashes/byte sizes and ID inventories |

Collection stages files before publishing the pointer. Failed enumeration does not
replace the last complete snapshot. Partial job/attempt/policy reads are represented
as gaps in a newly published snapshot, never filled from an older run's file. A hash
detects modification relative to the manifest, not a maliciously replaced manifest.
The provider reads are not a transaction; an attempt changing during collection is
flagged. Preserve previous snapshots for comparisons under the retention policy.

Requests are GET-only, serial, timed and bounded in retry/wait. `--incremental`
revalidates cached responses with the authenticated server; it does not reinterpret
failed requests as fresh evidence. Raw provider errors are sanitized. Exit 3 means
partial evidence; inspect the reason. Narrow an API-limited query and recollect.
Do not merge separate snapshots without checking run identity and collection time.

## Analysis and cohort identity

`analyze_gh.py` prefers the manifest-selected snapshot. Legacy `raw/` input remains
readable but explicitly unverified. Filtering applies to runs, jobs and attempts
together. Returned `ci-metrics/2` includes diagnostic latency proxies, counts of
unsuccessful/incomplete observations, actual attempt-based recovery, overlapped
step work versus elapsed coverage, sample uncertainty, and raw runner usage.

Use [cohort-context.example.json](../templates/cohort-context.example.json) with
`--cohort-context` to annotate run IDs with `trust`, `config`, `cache`, `workload`
and `runner_class`. These assertions need source evidence. Unknown attributes do
not support a comparable-workload claim. Coarse workflow summaries remain useful
for investigation, but do not replace the narrower cohort results.

`--pricing` accepts `ci-pricing/1`: `currency`, dated `as_of`, `source`, and a `rates`
array of `{labels, per_minute, billing_quantum_seconds}`. Match the complete sorted
runner label set. Estimates exclude plan allowances, storage, transfer and fixed
costs. Cost per merge additionally requires explicit `merged_prs`, exact `run_ids`
and `population_complete:true` for the same accounting population. Missing prices
or evidence remain unknown; rounded observed minutes are not the bill.

Other CI providers can supply the legacy run/job shapes described in
[measurement](04-measurement-benchmarking.md#raw-schema), or a conforming snapshot.
They must separately resolve check identity, source revision, attempts and policy.
No provider is claimed to have a native collector unless one is implemented.

## Dependency graphs

`--graph` accepts the [graph example](../templates/job-graph.example.json):
`schema:ci-job-graph/1`, `complete:true`, `matrix_resolved:true`, a nonempty
`required_nodes` list and `nodes` with unique `id`, `needs` and finite nonnegative
`duration_s`. Counterfactuals provide a name and replacement `durations_s` by node.

The helper computes the longest path to required terminals and recomputes it after
each intervention. The example is a 300-second build feeding a 5-second test; a
100-second build reduction gives 205 seconds. Shared ancestors are not double
counted. Cycles, missing nodes, unresolved expansion and incomplete graphs return
unknown. This model does not infer scheduler capacity, queue or correlated tails;
model those separately and validate predicted gains against actual runs.

## Benchmark context and comparability

Start with [benchmark-context.example.json](../templates/benchmark-context.example.json).
Replace its unknowns with reviewed `repository_id`, `workload_id`, a `toolchain`
version map, `cache_state` and `runner_class`. Do not put secrets or raw environment
variables in metadata. Source fingerprinting includes HEAD/index/non-ignored changes;
ignored build inputs and external dependencies need separate workload evidence.

```sh
python3 scripts/benchmark.py --label before --runs 20 --warmup 1 \
  --timeout 120 --metadata .ci-audit/benchmark-context.json \
  --out .ci-audit/experiments/before.json -- python3 -m unittest discover -s tests
```

For paired trials, pass `--candidate-command candidate-argv.json` containing a
reviewed JSON array, plus a candidate label/optional metadata. AB/BA ordering is
balanced with a reproducible seed. Both arms share one worktree; commands must not
mutate source or depend on order. Control shared caches explicitly. Compare separate
collections with `--baseline`; an intentional different-source comparison requires
matching `reviewed_comparison` assertions in both records: `experiment_id`, immutable
`baseline_fingerprint`, `candidate_fingerprint`, `allowed_difference` and
`review_reference`. This exception is not available for different-source paired arms.

Warmups are recorded. Failure aborts by default; `--allow-failures` does not make a
failed experiment comparable. Timeout/interruption cleans the process group before
another trial. Windows execution is explicitly unsupported; a reviewed container or
job supervisor is needed there. Processes that deliberately escape their group are
outside the helper's containment promise. Benchmark only reviewed non-production work.

Reports retain before/after provenance, every sample, cleanup results and comparison
warnings. Missing identity, raw failures, source changes or incomplete cleanup reject
an improvement claim. Fewer than ten successful samples are preliminary; p95 remains
conservative below forty samples, and no count alone proves precision. Bootstrap
intervals describe the measured samples, not unmeasured CI population variability.
Even a performance signal requires correctness/security/representative-CI gates.

## Workflow semantic input

```sh
python3 scripts/inspect_workflows.py --workflow normalized-workflow.json \
  --path .github/workflows/ci.yml --lockfile normalized-actions-lock.json \
  --context workflow-context.json --out workflow-inspection.json
```

Use a pinned YAML 1.2 parser with duplicate-key rejection, not an ad hoc regex
conversion. `--workflow` is the workflow object itself. `--lockfile` is the native
generated lockfile converted losslessly to JSON. Context supports
`trigger_cache_default`, `trigger_trust`, explicit `caller_cache_limit`, `event_name`,
`platform`, `host`, and `features` for `actions_lockfile`, `same_repo_refs`,
`parallel_steps`, `cache_mode`, `queue_max`. Missing capability context stays unknown.

Inspect classifications and findings; do not treat this advisory report as an
authoritative workflow acceptance gate. Dynamic expressions, caller expansion and
live action identity/reachability need the platform and exact source evidence.
Never invent a lockfile from regex hits. See [platform semantics](09-platform-updates.md).

## Four-category policy

Copy [validation-policy.example.json](../templates/validation-policy.example.json)
and have the repository owner review its expectations. It defines:

- Repository identity, PR feedback budget, minimum PR sample and maximum evidence age.
- All four categories, each with explicit qualified required-check identities.
- Optional per-check `allowed_skip_reasons`; a skip still needs verified selection.
- Integration mode (`merge_queue`, `rolling` or `all_pr`), maximum green age,
  maximum active-run age and maximum unverified-commit lag.
- Explicit rollout enablement. If enabled: increasing percentages ending at 100,
  minimum samples/window, freshness and absolute/relative metric thresholds.

Qualified identities must distinguish provider app/workflow/job/context, event and
repository where needed. Resolve them in the adapter; a matching display name alone
cannot grant a pass. `all_pr` records an existing full updated-branch policy; it does
not let a PR head certify a different final main revision.
One actual check may cover multiple categories when its reviewed obligations and
skip contract agree; the categories do not require separate jobs.

Get the canonical policy digest with `evaluate_validation.py --policy FILE
--policy-digest`. Every evidence record and required check must bind this digest.
A policy edit changes the digest and invalidates prior evidence under that policy.

## Validation evidence

[validation-evidence.example.json](../templates/validation-evidence.example.json)
is a **synthetic example**, not a production verification record. Its two PR samples
deliberately fail the policy's twenty-sample sufficiency requirement. The release
and rollout examples pass only at the example's declared evaluation time and after
the assumed external verification of their assertions.

`ci-validation-evidence/1` binds repository, policy digest and `observed_at`.
`pr_events_complete`/`integration_records_complete` must be explicit. Record exact
immutable revisions, event receipt and lifecycle state; never use a mutable branch
name as identity. Each required check includes identity, revision, policy digest,
attempt, conclusion, completion time and source. Check enumeration must be complete.
Later failures/attempts invalidate older greens; ambiguous tied events stay unknown.

Integration/release records must be `completed`. Full verification binds the exact
build-input digest. `integration_state` records main head, unverified commits and
active-run/start state; exceeding bounds requests backpressure/fix-forward work.
Queue-candidate/final-main equivalence is not inferred: verify the exact source again
if the provider adapter cannot prove equivalence under the accepted policy.

The release candidate additionally binds its immutable artifact digest. Each release
check must cover that artifact. Verified provenance must match source, inputs,
artifact and policy, with source/time evidence. The exact candidate's full-suite
record must still be fresh. A new rebuild cannot inherit a different artifact's proof.

`rollout_state` binds deployment ID, environment, source, artifact, current stage and
stage activation time. Observation identity must match; its window begins after
activation and satisfies age/duration/sample requirements. Relative thresholds need
verified comparable baseline source, context and window. Missing/inconclusive data
pauses promotion; an observed threshold breach aborts. A numeric pass does not
authorize deployment or replace feature-flag, migration and operational checks.

## Result consumption

```sh
python3 scripts/evaluate_validation.py \
  --policy .ci-audit/validation-policy.json \
  --evidence .ci-audit/validation-evidence.json \
  --output .ci-audit/validation-result.json --require release
```

Without `--require`, exit 0 means the report was written, **not that validation
passed**. Repeat `--require` for explicitly requested result gates. Unknown, blocked
or insufficient required results exit 1; invalid input exits 2. Top-level unknown
evidence cannot satisfy any required gate. `--now` is for reproducible historical
analysis; production adapters must use the actual current time.

Budget reporting retains latest-revision and first-push durations, completed-event
breaches, failed/running/cancelled/superseded counts and sample sufficiency. A breach
also includes a failed/cancelled event with verified `source` and `completed_at`,
or elapsed time on a still-running current event; neither becomes a green sample.
Missing completion evidence remains unknown. A breach
requests an agent investigation; this helper does not open PRs, deploy or install a
recurring automation. Operational integrations must enforce their own authorization.

## Report transport

```sh
python3 scripts/check_reports.py --root downloaded-reports \
  --expected-shards 1 2 3 4 --prefix blob-2- --pattern 'blob-*.json' \
  --stage-out validated-reports
```

Here `2` is the example **current run attempt**, not a hard-coded production value.
Download separate `blob-ATTEMPT-SHARD` artifacts from the exact workflow run. The
helper requires the exact expected directory set and valid bounded regular JSON
payloads, then stages uniquely named files without overwriting prior output.
Exit 1 blocks on missing/invalid reports; exit 2 indicates input/I/O failure. Run the
original test runner's merge and coverage gates afterwards. Verified transport is
necessary but insufficient for test success or coverage equivalence.
