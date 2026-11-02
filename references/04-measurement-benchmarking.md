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
2. `analyze_gh.py`, then read `last_finishing_job`, `job_duration_s`, `setup_share`, `shard_skew` to find the critical path.
3. Join the YAML `needs` DAG by hand for the slowest workflow; write `critical-path.md`.
4. Note the window, sample size, and excluded runs (cancelled, `skipped`, bots).

Compare like with like: same workflow, event and runner class. Never mix fork PRs with trusted pushes or releases.

## Experiments

- One variable per experiment. Freeze commit, toolchain, runner label.
- ≥10 runs per arm; fewer is `preliminary`. Interleave arms (A B A B) when the runner pool is noisy; the first run on a fresh runner is often slower.
- Measure cold and warm cache as separate arms.
- Local: `benchmark.py --label baseline --runs 10 --warmup 1 --out ... -- <cmd>`, then candidate with `--baseline`. The `comparison.warnings` list must be empty or explained.
- CI: push the candidate to a branch with `workflow_dispatch`, trigger N times, collect with `collect_gh.py --branch <exp-branch>`.

Decision: **ADOPT** when p50 and p95 both improve beyond run-to-run noise (rule of thumb: delta larger than the baseline's p95−p50 spread) and every gate in [the playbook](02-optimization-playbook.md) passes; **REJECT** on any correctness or security regression whatever the speed-up; otherwise **INSUFFICIENT EVIDENCE**.

Experiment record (`experiments/<id>.json`): id, hypothesis, module, variable, baseline file, candidate file, sample sizes, p50/p95 deltas, gates checked with result, decision, rollback.

## Monitoring and rollback

Re-run `collect_gh.py` + `analyze_gh.py` weekly, or after every merged optimization, and diff `metrics.json`.

Rollback when, versus the pre-change baseline over ≥20 runs: p95 push→green worse by >20%; flake rate up by >2 points; any false green or escaped defect attributed to the change; cost per merge up beyond what was approved. Rollback = revert the single PR (that is why changes ship one per PR).

## Raw schema

Other providers: write `raw/runs.json` as a list of `{id, name, workflow_id, event, head_sha, head_branch, conclusion, created_at, updated_at, run_attempt}` and `raw/jobs/<id>.json` as a list of `{name, conclusion, created_at, started_at, completed_at, run_attempt, labels, steps:[{name, started_at, completed_at}]}` with ISO-8601 UTC timestamps. `analyze_gh.py` then works unchanged.
