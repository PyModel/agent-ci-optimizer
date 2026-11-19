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
| [Cache economics](../scripts/analyze_cache.py) | Pass an input `ci-cache-events/1` JSON file with comparable cohorts, explicit outcomes, times and optional bytes. Unknown inputs stay unknown; savings are runner seconds, not a critical-path gain or a bill. |
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

### Remaining integration work

Provider fixtures cover the contracts of the new collectors; they are not live
provider verification runs. Native JUnit parsing, OpenTelemetry import/export,
calibration against real runner/host pressure and total ownership data, live
signature verification, and measured agent behavior remain separate work.
