#!/usr/bin/env python3
"""Turn collect_gh.py output into baseline CI metrics.

Every metric carries its definition and sample size. A metric with no data is
the string "unknown", never 0. Workflow-level numbers are approximations of
required-check time: jobs are tagged with whether branch rules require them
(from raw/required_checks.json), but the YAML `needs` DAG is not joined
(see limitations in the output).

Usage: python3 scripts/analyze_gh.py --input .ci-audit --output .ci-audit/metrics.json [--since ISO8601]
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

UNKNOWN = "unknown"
LOW_SAMPLE = 10
HEAVY_TAIL = 3.0  # p95/p50 above this: size by p95, explain the tail
GREEN = {"success", "skipped", "neutral"}
SETUP_STEP = re.compile(
    r"^(set up job|complete job|post |run actions/checkout|checkout|run actions/setup-|setup |"
    r"set up |run actions/cache|cache|restore cache|save cache|install( dependencies)?\b|"
    r"run (npm|pnpm|yarn|bun) (ci|install)|run pip install|run uv sync|run .*/setup-)",
    re.IGNORECASE,
)
MATRIX = re.compile(r"^(.*?) \((.+)\)$")

DEFINITIONS = {
    "run_wall_s": "run.created_at -> max(job.completed_at) of the latest attempt; includes queue. Falls back to run.updated_at when jobs are missing.",
    "job_queue_s": "job.started_at - job.created_at, per job.",
    "job_duration_s": "job.completed_at - job.started_at, per job name.",
    "setup_share": "sum(setup-like step durations) / sum(step durations), per job; step names matched by regex.",
    "push_to_green_s": "per head_sha: earliest run created_at -> last run completion, only when every workflow's latest attempt for that sha is green. Not required-check aware.",
    "flaky_sha_rate": "share of (workflow, head_sha) pairs with >1 run whose conclusions include both failure and success.",
    "rerun_rate": "share of runs with run_attempt > 1.",
    "shard_skew": "per run and matrix family: slowest shard duration / median shard duration.",
    "billable_minutes_by_label": "sum over jobs of ceil(duration/60), grouped by runner label set. Not a bill: no price, no minute multipliers.",
    "last_finishing_job": "per workflow: runs in which this job completed last (latest attempt, skipped jobs excluded); required = its name is a required status check context (unknown when branch rules could not be read).",
    "gap_to_runner_up_s": "per successful run: last job completed_at - second-to-last job completed_at. The most wall-clock a fix to the last job alone can save before another job gates.",
    "first_pass_failure_rate": "share of success/failure runs that failed or needed run_attempt > 1. time_to_green_multiplier = 1 / (1 - rate).",
    "heavy_tail": "set on a distribution when p95 / p50 > 3.",
}
LIMITATIONS = [
    "The `needs` DAG is not joined, and required tagging matches job names to check contexts exactly; workflow time approximates, not equals, merge-blocking time.",
    "Cache hit/miss and test counts need job logs; reported as unknown.",
    "Cost needs runner pricing and plan details; only billable-minute counts are given.",
    "Runs are grouped by workflow name + event; renamed workflows split into separate groups.",
    "Matrix families (shard_skew) are detected only from default `job (a, b)` names; custom matrix job names report unknown.",
    "Non-CI events (issue_comment, schedule, pull_request_target bots) are included unless collect_gh.py --event filtered them.",
]


def ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def secs(a: str | None, b: str | None) -> float | None:
    start, end = ts(a), ts(b)
    if start is None or end is None or end < start:
        return None
    return (end - start).total_seconds()


def pct(values: list[float], q: float) -> float:
    """Nearest-rank percentile; values must be non-empty."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q / 100 * len(ordered)) - 1)]


def dist(values: list[float]) -> dict | str:
    vals = [v for v in values if v is not None]
    if not vals:
        return UNKNOWN
    p50, p95 = pct(vals, 50), pct(vals, 95)
    out = {"n": len(vals), "p50": round(p50, 3), "p95": round(p95, 3),
           "mean": round(statistics.fmean(vals), 3), "max": round(max(vals), 3)}
    if p50 > 0 and p95 / p50 > HEAVY_TAIL:
        out["heavy_tail"] = True
    if len(vals) < LOW_SAMPLE:
        out["low_sample"] = True
    return out


def ratio(num: int, den: int) -> dict | str:
    if den == 0:
        return UNKNOWN
    out = {"value": round(num / den, 4), "num": num, "den": den}
    if den < LOW_SAMPLE:
        out["low_sample"] = True
    return out


def load(input_dir: Path) -> tuple[list[dict], dict[int, list[dict]], dict, set[str] | None]:
    raw = input_dir / "raw"
    runs_path = raw / "runs.json"
    if not runs_path.is_file():
        raise FileNotFoundError(f"{runs_path} not found; run collect_gh.py first")
    runs = json.loads(runs_path.read_text())
    jobs: dict[int, list[dict]] = {}
    for p in (raw / "jobs").glob("*.json"):
        if p.stem.isdigit():
            jobs[int(p.stem)] = json.loads(p.read_text())
    meta_path = raw / "collect_meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
    req_path = raw / "required_checks.json"
    required = required_contexts(json.loads(req_path.read_text())) if req_path.is_file() else None
    return runs, jobs, meta, required


def required_contexts(required: dict) -> set[str] | None:
    """Required status-check contexts from collect_gh.py's required_checks.json.
    None when either source could not be read (403 etc.); a 404 on classic
    protection means none configured."""
    names: set[str] = set()
    classic = required.get("classic_protection")
    if not isinstance(classic, dict):
        return None
    if "unavailable" in classic:
        if "404" not in classic["unavailable"]:
            return None
    else:
        names.update(c for c in classic.get("contexts") or [] if isinstance(c, str))
        names.update(c["context"] for c in classic.get("checks") or [] if isinstance(c, dict) and c.get("context"))
    rules = required.get("rulesets")
    if not isinstance(rules, list):
        return None
    for rule in rules:
        params = rule.get("parameters") if isinstance(rule, dict) else None
        for c in (params or {}).get("required_status_checks") or []:
            if isinstance(c, dict) and c.get("context"):
                names.add(c["context"])
    return names


def latest_attempt(run: dict, jobs: list[dict]) -> list[dict]:
    attempt = run.get("run_attempt") or max((j.get("run_attempt") or 1 for j in jobs), default=1)
    return [j for j in jobs if (j.get("run_attempt") or 1) == attempt]


def run_end(run: dict, jobs: list[dict]) -> str | None:
    ends = [j["completed_at"] for j in latest_attempt(run, jobs) if j.get("completed_at")]
    return max(ends, key=lambda s: ts(s)) if ends else run.get("updated_at")


def analyze_group(runs: list[dict], jobs_by_run: dict[int, list[dict]], required: set[str] | None = None) -> dict:
    conclusions = Counter(r.get("conclusion") or "none" for r in runs)
    green_runs = [r for r in runs if r.get("conclusion") == "success"]
    wall = [secs(r.get("created_at"), run_end(r, jobs_by_run.get(r["id"], []))) for r in green_runs]
    queue, setup_share, skew, gaps = [], [], [], []
    job_durations: dict[str, list[float]] = defaultdict(list)
    slowest = Counter()
    for r in runs:
        jobs = latest_attempt(r, jobs_by_run.get(r["id"], []))
        families: dict[str, list[float]] = defaultdict(list)
        ends: list[tuple[datetime, str]] = []
        for j in jobs:
            queue.append(secs(j.get("created_at"), j.get("started_at")))
            if j.get("conclusion") == "skipped":
                continue
            d = secs(j.get("started_at"), j.get("completed_at"))
            if d is None:
                continue
            name = j.get("name", "?")
            if r.get("conclusion") == "success":
                job_durations[name].append(d)
            m = MATRIX.match(name)
            if m:
                families[m.group(1)].append(d)
            ends.append((ts(j.get("completed_at")), name))
            steps = [(s.get("name", ""), secs(s.get("started_at"), s.get("completed_at"))) for s in j.get("steps") or []]
            total = sum(x for _, x in steps if x)
            if total > 0:
                setup_share.append(sum(x for n, x in steps if x and SETUP_STEP.match(n)) / total)
        ends.sort()
        if ends:
            slowest[ends[-1][1]] += 1
        if len(ends) >= 2 and r.get("conclusion") == "success":  # a failed run ends at the failure, not the floor
            gaps.append((ends[-1][0] - ends[-2][0]).total_seconds())
        for durations in families.values():
            if len(durations) >= 2 and statistics.median(durations) > 0:
                skew.append(max(durations) / statistics.median(durations))
    return {
        "runs": len(runs),
        "conclusions": dict(conclusions),
        "run_wall_s_success": dist(wall),
        "job_queue_s": dist(queue),
        "setup_share": dist(setup_share),
        "shard_skew": dist(skew),
        "rerun_rate": ratio(sum(1 for r in runs if (r.get("run_attempt") or 1) > 1), len(runs)),
        "last_finishing_job": {
            n: {"runs": c, "required": UNKNOWN if required is None else n in required}
            for n, c in slowest.most_common(5)
        } or UNKNOWN,
        "gap_to_runner_up_s": dist(gaps),
        "job_duration_s": {n: dist(v) for n, v in sorted(job_durations.items(), key=lambda kv: -pct(kv[1], 50))[:15]},
        "jobs_missing_for_runs": sum(1 for r in runs if r["id"] not in jobs_by_run),
    }


def push_to_green(runs: list[dict], jobs_by_run: dict[int, list[dict]]) -> dict | str:
    by_sha: dict[str, list[dict]] = defaultdict(list)
    for r in runs:
        if r.get("event") in {"push", "pull_request", "merge_group"} and r.get("head_sha"):
            by_sha[r["head_sha"]].append(r)
    out = []
    for group in by_sha.values():
        latest: dict = {}
        for r in group:  # latest attempt per workflow
            key = r.get("workflow_id") or r.get("name")
            if key not in latest or (r.get("run_attempt") or 1) >= (latest[key].get("run_attempt") or 1):
                latest[key] = r
        if all(r.get("conclusion") in GREEN for r in latest.values()):
            start = min((r["created_at"] for r in group), key=ts)
            end = max((run_end(r, jobs_by_run.get(r["id"], [])) or r["created_at"] for r in latest.values()), key=ts)
            out.append(secs(start, end))
    return dist(out)


def first_pass(runs: list[dict]) -> dict | str:
    done = [r for r in runs if r.get("conclusion") in {"success", "failure"}]
    failed = sum(1 for r in done if r.get("conclusion") == "failure" or (r.get("run_attempt") or 1) > 1)
    out = ratio(failed, len(done))
    if isinstance(out, dict):
        out["time_to_green_multiplier"] = round(1 / (1 - out["value"]), 3) if out["value"] < 1 else UNKNOWN
    return out


def flaky(runs: list[dict]) -> dict | str:
    pairs: dict[tuple, set] = defaultdict(set)
    counts: Counter = Counter()
    for r in runs:
        key = (r.get("name"), r.get("head_sha"))
        pairs[key].add(r.get("conclusion"))
        counts[key] += 1
    multi = [k for k in pairs if counts[k] > 1]
    hits = [k for k in multi if {"failure", "success"} <= pairs[k]]
    return ratio(len(hits), len(multi))


def billable(jobs_by_run: dict[int, list[dict]]) -> dict | str:
    minutes: Counter = Counter()
    for jobs in jobs_by_run.values():
        for j in jobs:
            d = secs(j.get("started_at"), j.get("completed_at"))
            if d:
                minutes[",".join(sorted(j.get("labels") or ["?"]))] += math.ceil(d / 60)
    return dict(minutes) or UNKNOWN


def analyze(runs: list[dict], jobs_by_run: dict[int, list[dict]], meta: dict,
            required: set[str] | None = None) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in runs:
        groups[f"{r.get('name', '?')} [{r.get('event', '?')}]"].append(r)
    created = sorted((r["created_at"] for r in runs if r.get("created_at")), key=ts)
    return {
        "schema": "ci-metrics/1",
        "source": meta.get("source", "collect_gh.py raw/"),
        "repo": meta.get("repo", UNKNOWN),
        "collected_at": meta.get("collected_at", UNKNOWN),
        "window": {"from": created[0], "to": created[-1]} if created else UNKNOWN,
        "since_filter": meta.get("since"),
        "sample": {"runs": len(runs), "runs_with_jobs": sum(1 for r in runs if r["id"] in jobs_by_run),
                   "collect_errors": len(meta.get("errors", []))},
        "push_to_green_s": push_to_green(runs, jobs_by_run),
        "flaky_sha_rate": flaky(runs),
        "first_pass_failure_rate": first_pass(runs),
        "required_checks": sorted(required) if required is not None else UNKNOWN,
        "billable_minutes_by_label": billable(jobs_by_run),
        "cache_hit_rate": UNKNOWN,
        "cost_per_merge": UNKNOWN,
        "workflows": {k: analyze_group(v, jobs_by_run, required) for k, v in sorted(groups.items())},
        "definitions": DEFINITIONS,
        "limitations": LIMITATIONS,
    }


def summary_lines(m: dict) -> list[str]:
    lines = [f"repo={m['repo']} runs={m['sample']['runs']} window={m['window']}",
             f"push_to_green_s={m['push_to_green_s']}", f"flaky_sha_rate={m['flaky_sha_rate']}"]
    for name, g in m["workflows"].items():
        w = g["run_wall_s_success"]
        lines.append(f"  {name}: n={g['runs']} wall_p50={w if w == UNKNOWN else w['p50']} "
                     f"wall_p95={w if w == UNKNOWN else w['p95']} last_job={next(iter(g['last_finishing_job']), '-') if isinstance(g['last_finishing_job'], dict) else '-'}")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compute CI baseline metrics from collect_gh.py output.")
    ap.add_argument("--input", type=Path, default=Path(".ci-audit"), help="collect_gh.py --out directory")
    ap.add_argument("--output", type=Path, default=Path(".ci-audit/metrics.json"))
    ap.add_argument("--since", help="only runs created at or after this ISO-8601 time (e.g. the last workflow-config change)")
    args = ap.parse_args(argv)
    since = ts(args.since) if args.since else None
    if args.since and (since is None or since.tzinfo is None):
        ap.error("--since must be ISO-8601 with a timezone, e.g. 2026-10-01T00:00:00Z")
    try:
        runs, jobs, meta, required = load(args.input)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if since:
        runs = [r for r in runs if (created := ts(r.get("created_at"))) and created >= since]
        meta = {**meta, "since": args.since}
    metrics = analyze(runs, jobs, meta, required)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(metrics, indent=2))
    print("\n".join(summary_lines(metrics)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
