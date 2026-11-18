# Measurement and benchmarking

## Metric definitions

| Metric | Definition | Source |
|---|---|---|
| push→green | verified event receipt → authoritative required-check completion for the exact current revision | compute from expected checks and raw events/attempts; analyzer output is a diagnostic proxy, not required-check aware |
| queue | job `started_at − created_at` | jobs API |
| setup | checkout + toolchain + cache + install steps | step timings, regex-classified |
| execution | elapsed execution excluding non-overlapping setup coverage; retain summed step work separately | jobs/step interval evidence |
| critical path | longest chain through the resolved dependency DAG to required terminals | reviewed normalized graph + job timings; counterfactual recomputation |
| suspected retry recovery | observed same-run failure followed by success; not proven flakiness | actual attempt history; inspect logs and intentional reruns |
| cache hit rate | restore hits / restore attempts | job logs (`Cache restored from key`) — not in API |
| cost per merge | accounted runner cost / merged PRs for the same complete population | dated explicit label/rate/quantum + population identity; observed-minute estimates are not bills |

Unknown stays `unknown`. A report that writes `0` for a missing cache hit rate is wrong.

The collector publishes immutable snapshots with explicit payload inventories and
completeness. The analyzer checks manifests, filters the complete run/job/attempt
cohort, retains successful predecessors on partial reruns, and separates unknown
first attempts from observed failures. It never treats a skipped/neutral run as
observed green. These repairs make diagnostics reliable within their stated scope;
they do not turn raw job timing into an authoritative gate ledger.

Use `evaluate_validation.py` for the four-category policy and exact evidence
contract. Its provider-normalized input must already be authenticated and verified.
The tool checks identity, chronology, coverage completeness, freshness and policy
bounds; it does not retrieve or cryptographically verify attestations. See
[evidence formats](11-evidence-formats.md) for inputs and exit-code consumption.

## Baseline procedure

1. `collect_gh.py --limit 150` (filter `--event pull_request --target-branch main` for PRs, `--event push --branch main` for main). Add `--include-incomplete` to retain waiting/running states. Split capped date windows explicitly; partial evidence stays partial.
2. `analyze_gh.py`, then inspect job/step distributions, interval-union setup, shard skew, actual attempt history and cohort results. `last_finishing_job` and the runner-up gap are investigative leads; name-only required matches are explicitly weak.
3. Resolve YAML/matrices/reusable definitions into `ci-job-graph/1`; pass `--graph` for longest-required-path and counterfactual analysis. Write the evidence and assumptions in `critical-path.md`.
4. Note the window, sample sizes and counts of failed, cancelled, skipped, waiting and unfinished observations. Do not hide slow failures by reporting only successful duration statistics.
5. Config eras: `git log -1 --format=%cI -- .github/workflows/<file>` inside the window means the sample mixes old and new config. Re-run `analyze_gh.py --since <that commit date>`; if too few runs remain, say the numbers describe the old config.

Compare like with like: same workflow, event and runner class. Never mix fork PRs with trusted pushes or releases.

## Sizing and combining savings

Size every candidate on two axes: **Δ wall-clock** (p50 and p95 of push→green or of the required path) and **Δ runner-minutes** (bill and capacity). They are different wins; report both and rank speed work by the first.

- **Dependency rule.** Recompute the longest required path after each change. A 300-second build feeding a 5-second test becomes a 205-second path when the build is reduced by 100 seconds, even though the build was not last to finish. `gap_to_runner_up_s` is only an observation; it bounds an isolated improvement only for independent competing terminal branches with unchanged scheduling. Aggregate jobs and shared ancestors invalidate a universal finish-gap rule.
- **Non-additive.** Per-change wall-clock deltas do not add. Two fixes on the same pole overlap; a fix below the floor counts only once the jobs above it also drop. Two near-equal concurrent jobs (300 s and 299 s) each save about nothing alone and 100 s together. Derive the combined figure from a stacked model (re-compute the path with all changes applied), never by summing. Runner-minute deltas do add, since they remove disjoint step time.
- **Tails.** When p95/p50 > 3 (`heavy_tail` in `metrics.json`) or mean/p50 > 1.5, size by p95 and name the slow run's cause from its log; otherwise say the tail is unexplained. Sum runner-minutes from the mean, not p50.
- **Reliability affects wait.** Measure observed attempts and repair/approval delays. The factor `1/(1−f)` is only a hypothetical model for independent, equal-duration retry attempts, not an observed time-to-green estimate. Intentional successful reruns are not failures; failure-to-success without diagnosis is suspected flakiness, not proof.
- **Never zero after the fix.** A cache restore or artifact download still costs seconds; subtract the measured remainder, not the whole step.

## Evidence tiers

A pattern match in YAML is a lead, not a finding. A **finding** names a specific cause, shows positive evidence that it costs time in this repo's runs, and points at a change someone can make. "No cache line in the log" does not prove a cold cache; "fix your flaky test" is a reliability note, not a CI change.

| Evidence | Interpretation | Reporting |
|---|---|---|
| Fewer than 10 comparable duration samples | preliminary | show observations; do not claim an established speed-up |
| At least 10 comparable duration samples | candidate comparison | show uncertainty, workload, window and representativeness; p95 remains preliminary below 40 |
| Fewer than 30 observations for a rate | low sample | show numerator/denominator and uncertainty; do not assert stable reliability |
| At least 30 observations for a rate | interpretable with qualifications | sample count alone does not establish stability or causation |
| Missing or incompatible evidence | unknown | state the next measurement; never invent a saving |

A "biggest cost" or "dominant step" claim lists the top five steps by p50 with run counts, or says "largest addressable" and names what was excluded.

## Experiments

- One variable per experiment. Freeze and record source before/after, toolchain, runner and workload identity. A different-source comparison requires reviewed immutable fingerprints and a scoped allowed difference.
- ≥10 runs per arm; fewer is `preliminary`. Interleave arms (A B A B) when the runner pool is noisy; the first run on a fresh runner is often slower.
- Measure cold and warm cache as separate arms.
- Local: `benchmark.py --label baseline --runs 10 --warmup 1 --metadata context.json --out ... -- <cmd>`, then candidate with `--baseline`, or balanced paired argv inputs. Recorded warmup/sample failures, changed provenance or incomplete process-group cleanup invalidate comparability. Bootstrap intervals and warnings must be interpreted, not suppressed.
- CI: push the candidate to a branch with `workflow_dispatch`, trigger N times, collect with `collect_gh.py --branch <exp-branch>`.

Decision: **ADOPT** only after representative measurements support the improvement
with stated uncertainty and every gate in [the playbook](02-optimization-playbook.md)
passes. A p95−p50 spread is not a confidence interval. The benchmark helper reports
performance signals, never automatic adoption. **REJECT** any correctness/security
regression; use **INSUFFICIENT EVIDENCE** when comparability or precision is missing.

Experiment record (`experiments/<id>.json`): id, hypothesis, module, variable, baseline file, candidate file, sample sizes, p50/p95 deltas, workload state, gating job before → after, gates checked with result, decision, rollback.

## CI-side verification

For a change measured on real CI rather than with `benchmark.py`:

- Measure push-wide with a stated cohort: earliest relevant event/run receipt → complete authoritative gate for the exact revision, resolving workflows and attempts from raw evidence. `push_to_green_s` is an approximate exploratory summary; verify it independently and name timestamp proxies. Never sum workflow durations or average per-job ratios.
- Re-run the candidate SHA at least twice; if two runs differ by more than 20% of their median, go to four. Keep the cold first run in the set and report median and range. One rerun of an unchanged SHA in the same window is the noise control.
- Disclose the asymmetry: the baseline spans many commits, the candidate is a few reruns of one.
- **Workload state.** Record one of `same`, `reduced`, `increased`, `changed`, `unknown` (default). Only `same`, backed by matching job and test identities, supports "same work, faster". Equal job counts alone are `unknown`; re-sharding is `changed`, not `reduced`. For `increased`, `changed` or `unknown` the delta is not a lower bound on the benefit. A branch that also changes product code cannot be attributed to CI alone.
- **Gate migration.** Re-derive which job finishes last after the change. If the gate moved to another job, report the new gate and its time, not the old pole's gain as merge-wait saved.

## Monitoring and rollback

Use the [PR feedback budget](08-validation-architecture.md#pr-feedback-budget) when
designing the four-category pipeline. Record target and observations separately;
the proposed 300-second objective covers latest-revision end-to-end PR feedback,
not just test execution. Keep first-push journey, completed-run breaches, failed or
unfinished observations and sample uncertainty visible. A breach opens an agent
investigation or optimization proposal; no runtime limit may certify unfinished work.
For redistribution, also track full-suite lag, last eligible green age, queue depth,
starvation and total runner time across all categories. Preserve accepted protection
when the budget cannot yet be met.

Propose collection/analysis after each merged optimization and at the agreed
monitoring cadence. Do not install a recurring job without authorization. Compare
compatible snapshots and policies; missing retained data cannot become zeros.

Rollback when, versus the pre-change baseline over ≥20 runs: p95 push→green worse by >20%; flake rate up by >2 points; any false green or escaped defect attributed to the change; cost per merge up beyond what was approved. Rollback = revert the single PR (that is why changes ship one per PR).

## Raw schema

Other providers can write legacy `raw/runs.json` as a list of `{id, name, workflow_id,
event, head_sha, head_branch, status, conclusion, created_at, updated_at, run_attempt}`
and `raw/jobs/<id>.json` as a list of `{id, name, conclusion, created_at, started_at,
completed_at, run_attempt, labels, steps:[{name, started_at, completed_at}]}` with
timezone-aware timestamps. Legacy freshness remains unverified. Prefer the current
manifest/attempt schema in [evidence formats](11-evidence-formats.md), preserving
the provider's identity distinctions. `updated_at` alone is not proof of green or
check completion; missing attempt and policy evidence remains unknown.
