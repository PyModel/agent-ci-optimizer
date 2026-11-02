# Measurement and benchmarking

## Metric definitions

| Metric | Definition | Source |
|---|---|---|
| push→green | first run created for a SHA → last required check success for that SHA | `analyze_gh.py` approximates with all workflows of push/PR/merge_group events |
| queue | job `started_at − created_at` | jobs API |
| setup | checkout + toolchain + cache + install steps | step timings, regex-classified |
| execution | job time minus setup | jobs API |
| critical path | longest chain through the `needs` DAG, using job durations | YAML DAG + job timings (manual join) |
| flake rate | share of SHAs with both failure and success for the same workflow | runs API; confirm by rerun logs |
| cache hit rate | restore hits / restore attempts | job logs (`Cache restored from key`) — not in API |
| cost per merge | runner minutes × price per label ÷ merged PRs in window | billing + plan; `billable_minutes_by_label` is the input |

Unknown stays `unknown`. A report that writes `0` for a missing cache hit rate is wrong.

## Baseline procedure

1. `collect_gh.py --limit 150` (filter `--event pull_request` for PR loop, `--event push --branch main` for main).
2. `analyze_gh.py`, then read `last_finishing_job` (how often each job finished last, tagged `required`), `gap_to_runner_up_s`, `job_duration_s`, `setup_share`, `shard_skew` to find the critical path.
3. Join the YAML `needs` DAG by hand for the slowest workflow; write `critical-path.md`.
4. Note the window, sample size, and excluded runs (cancelled, `skipped`, bots).
5. Config eras: `git log -1 --format=%cI -- .github/workflows/<file>` inside the window means the sample mixes old and new config. Re-run `analyze_gh.py --since <that commit date>`; if too few runs remain, say the numbers describe the old config.

Compare like with like: same workflow, event and runner class. Never mix fork PRs with trusted pushes or releases.

## Sizing and combining savings

Size every candidate on two axes: **Δ wall-clock** (p50 and p95 of push→green or of the required path) and **Δ runner-minutes** (bill and capacity). They are different wins; report both and rank speed work by the first.

- **Floor rule.** Shaving the job that finishes last helps only until the next-latest job becomes the gate: the most a pole-only fix can save is `gap_to_runner_up_s`. A job that already finishes before the pole saves **zero** wall-clock however much faster it gets. Label it runner-minutes only and keep it out of the speed ranking.
- **Non-additive.** Per-change wall-clock deltas do not add. Two fixes on the same pole overlap; a fix below the floor counts only once the jobs above it also drop. Two near-equal concurrent jobs (300 s and 299 s) each save about nothing alone and 100 s together. Derive the combined figure from a stacked model (re-compute the path with all changes applied), never by summing. Runner-minute deltas do add, since they remove disjoint step time.
- **Tails.** When p95/p50 > 3 (`heavy_tail` in `metrics.json`) or mean/p50 > 1.5, size by p95 and name the slow run's cause from its log; otherwise say the tail is unexplained. Sum runner-minutes from the mean, not p50.
- **Reliability multiplies wait.** A first-pass failure rate `f` stretches effective time-to-green by about `1/(1−f)` for the affected share (`first_pass_failure_rate` in `metrics.json`). A flaky required job can be a bigger speed lever than any cache.
- **Never zero after the fix.** A cache restore or artifact download still costs seconds; subtract the measured remainder, not the whole step.

## Evidence tiers

A pattern match in YAML is a lead, not a finding. A **finding** names a specific cause, shows positive evidence that it costs time in this repo's runs, and points at a change someone can make. "No cache line in the log" does not prove a cold cache; "fix your flaky test" is a reliability note, not a CI change.

| Runs behind the claim | Status | Number allowed |
|---|---|---|
| ≥30 (rates, flakes, failure share) or ≥10 (durations) | finding | yes, with n and window |
| 5–29 | probe | no saving quoted; say what data would confirm it |
| <5, or the workflow did not run in the window | dropped | no |

A "biggest cost" or "dominant step" claim lists the top five steps by p50 with run counts, or says "largest addressable" and names what was excluded.

## Experiments

- One variable per experiment. Freeze commit, toolchain, runner label.
- ≥10 runs per arm; fewer is `preliminary`. Interleave arms (A B A B) when the runner pool is noisy; the first run on a fresh runner is often slower.
- Measure cold and warm cache as separate arms.
- Local: `benchmark.py --label baseline --runs 10 --warmup 1 --out ... -- <cmd>`, then candidate with `--baseline`. The `comparison.warnings` list must be empty or explained.
- CI: push the candidate to a branch with `workflow_dispatch`, trigger N times, collect with `collect_gh.py --branch <exp-branch>`.

Decision: **ADOPT** when p50 and p95 both improve beyond run-to-run noise (rule of thumb: delta larger than the baseline's p95−p50 spread) and every gate in [the playbook](02-optimization-playbook.md) passes; **REJECT** on any correctness or security regression whatever the speed-up; otherwise **INSUFFICIENT EVIDENCE**.

Experiment record (`experiments/<id>.json`): id, hypothesis, module, variable, baseline file, candidate file, sample sizes, p50/p95 deltas, workload state, gating job before → after, gates checked with result, decision, rollback.

## CI-side verification

For a change measured on real CI rather than with `benchmark.py`:

- Measure push-wide: earliest run `created_at` → last job `completed_at` over every workflow the SHA triggered (what `push_to_green_s` computes). Never sum workflow durations or average per-job ratios.
- Re-run the candidate SHA at least twice; if two runs differ by more than 20% of their median, go to four. Keep the cold first run in the set and report median and range. One rerun of an unchanged SHA in the same window is the noise control.
- Disclose the asymmetry: the baseline spans many commits, the candidate is a few reruns of one.
- **Workload state.** Record one of `same`, `reduced`, `increased`, `changed`, `unknown` (default). Only `same`, backed by matching job and test identities, supports "same work, faster". Equal job counts alone are `unknown`; re-sharding is `changed`, not `reduced`. For `increased`, `changed` or `unknown` the delta is not a lower bound on the benefit. A branch that also changes product code cannot be attributed to CI alone.
- **Gate migration.** Re-derive which job finishes last after the change. If the gate moved to another job, report the new gate and its time, not the old pole's gain as merge-wait saved.

## Monitoring and rollback

Re-run `collect_gh.py` + `analyze_gh.py` weekly, or after every merged optimization, and diff `metrics.json`.

Rollback when, versus the pre-change baseline over ≥20 runs: p95 push→green worse by >20%; flake rate up by >2 points; any false green or escaped defect attributed to the change; cost per merge up beyond what was approved. Rollback = revert the single PR (that is why changes ship one per PR).

## Raw schema

Other providers: write `raw/runs.json` as a list of `{id, name, workflow_id, event, head_sha, head_branch, conclusion, created_at, updated_at, run_attempt}` and `raw/jobs/<id>.json` as a list of `{name, conclusion, created_at, started_at, completed_at, run_attempt, labels, steps:[{name, started_at, completed_at}]}` with ISO-8601 UTC timestamps. `analyze_gh.py` then works unchanged.
