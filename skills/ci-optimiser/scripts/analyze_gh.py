#!/usr/bin/env python3
"""Analyze immutable CI snapshots or explicitly unverified legacy raw data.

All latency values are diagnostic proxies, not authoritative merge/release gates.
Optional --graph, --cohort-context, --phase-map and --pricing inputs declare their assumptions.
Missing evidence remains unknown; a partial sample is never certified as complete.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from collect_gh import GhError, check_source_identity
from graph_metrics import evaluate_graph
import phase_map as reviewed_phases

UNKNOWN = "unknown"
NO_INTEGRITY_EXPECTATION = object()
LOW_SAMPLE = 10
RATE_MIN_SAMPLE = 30
HEAVY_TAIL = 3.0
GREEN = {"success"}
FAILURES = {"failure", "timed_out", "startup_failure"}
SETUP_STEP = re.compile(
    r"^(set up job|complete job|post |run actions/checkout|checkout|run actions/setup-|setup |"
    r"set up |run actions/cache|cache|restore cache|save cache|install( dependencies)?\b|"
    r"run (npm|pnpm|yarn|bun) (ci|install)|run pip install|run uv sync|run .*/setup-)", re.IGNORECASE)
MATRIX = re.compile(r"^(.*?) \((.+)\)$")
COHORT_FIELDS = ("trust", "config", "cache", "workload", "runner_class")
DEFINITIONS = {
    "run_wall_s": "original run.created_at to latest effective job completion; includes rerun waiting, not attempt execution alone; no updated_at fallback",
    "job_queue_s": "job.started_at minus job.created_at; no substitute for missing timestamps",
    "setup_share": "legacy alias of setup_work_share: summed setup step time / summed step time; not wall-clock overhead",
    "setup_wall_share": "union of observed setup step intervals clipped to job interval / job duration; may overlap execution",
    "push_to_green_s": "observed-success diagnostic proxy per SHA/event/ref/PR context: earliest observed run creation to latest success of each observed workflow; missing/ambiguous job evidence excluded; not a required-gate certificate",
    "flaky_sha_rate": "deprecated alias of suspected_retry_recovery_rate; observed failure-to-success attempts are not proof of flakiness",
    "first_pass_failure_rate": "actual first-attempt failure share among observed success/failure/timeout outcomes; missing attempt history excluded and counted",
    "rerun_rate": "share of runs with a known positive integer run_attempt greater than one; unknown attempt identities excluded and counted",
    "billable_minutes_by_label": "legacy alias of rounded_runner_minutes_estimate_by_label; ceil(job seconds/60), not an invoice",
    "gap_to_runner_up_s": "observed last two job completion-time gap; not a general upper bound on optimization savings",
    "cancelled_run_runner_seconds": "observed runner time belonging to cancelled runs; potential waste proxy, not proof all work was wasted",
    "job_phases": "per-job observed phase distributions over all attempts, with first attempts and reruns reported separately; incomplete phase evidence stays unknown",
}
LIMITATIONS = [
    "This analyzer is diagnostic and cannot certify the PR feedback budget, required-check completeness or release eligibility.",
    "Observed workflows may omit never-triggered, inaccessible or retention-deleted checks; even an immutable snapshot is not complete event history.",
    "Job-name correlation across partial reruns is a fallback; duplicate logical identities make success latency unknown.",
    "Required context matches are name evidence, not verification of app-bound checks, target-branch applicability or required workflows.",
    "Unknown cohort attributes prevent claims that compared runs used equivalent work, cache, trust or configuration.",
    "Without a reviewed phase map, step classification is a preliminary name heuristic. Map bindings are caller evidence, not independent workflow authentication.",
    "Nearest-rank percentiles are descriptive; sample thresholds do not establish significance. Correlated runs violate independent-sample assumptions.",
    "Graph counterfactuals model precedence with unlimited capacity, not queueing, resource contention or unexpanded workflow conditions.",
    "Runner-minute and cost estimates require explicit pricing and scope; no plan allowances, public-repo exemptions or self-hosted prices are inferred.",
]


def ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def secs(a: object, b: object) -> float | None:
    start, end = ts(a), ts(b)
    return (end - start).total_seconds() if start is not None and end is not None and end >= start else None


def finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q / 100 * len(ordered)) - 1)]


def dist(values: list) -> dict | str:
    vals = [v for v in values if finite(v) and v >= 0]
    if not vals:
        return UNKNOWN
    p50, p95 = pct(vals, 50), pct(vals, 95)
    out = {"n": len(vals), "p50": round(p50, 3), "p95": round(p95, 3),
           "mean": round(statistics.fmean(vals), 3), "max": round(max(vals), 3),
           "method": "nearest-rank", "p95_tail_observations": len(vals) - math.ceil(.95 * len(vals)) + 1,
           "inference": "descriptive; no significance claim"}
    if p50 > 0 and p95 / p50 > HEAVY_TAIL:
        out["heavy_tail"] = True
    if len(vals) < LOW_SAMPLE:
        out["low_sample"] = True
    return out


def ratio(num: int, den: int) -> dict | str:
    if not den:
        return UNKNOWN
    p, z = num / den, 1.959963984540054
    scale = 1 + z * z / den
    center = (p + z * z / (2 * den)) / scale
    margin = z * math.sqrt(p * (1 - p) / den + z * z / (4 * den * den)) / scale
    out = {"value": round(p, 4), "num": num, "den": den,
           "wilson_95": [round(max(0, center - margin), 6), round(min(1, center + margin), 6)],
           "interval_assumption": "independent Bernoulli observations; descriptive if correlated"}
    if den < RATE_MIN_SAMPLE:
        out["low_sample"] = True
    return out


def read_json(base: Path, relative: str, expected: object = NO_INTEGRITY_EXPECTATION) -> object:
    path = (base / relative).resolve()
    try:
        path.relative_to(base.resolve())
    except ValueError as exc:
        raise ValueError(f"path escapes input directory: {relative}") from exc
    payload = path.read_bytes()
    if expected is not NO_INTEGRITY_EXPECTATION and (not isinstance(expected, dict)
                                 or type(expected.get("bytes")) is not int or expected["bytes"] < 0
                                 or not isinstance(expected.get("sha256"), str)
                                 or not re.fullmatch(r"[0-9a-f]{64}", expected["sha256"])
                                 or expected["bytes"] != len(payload)
                                 or expected["sha256"] != hashlib.sha256(payload).hexdigest()):
        raise ValueError(f"snapshot payload integrity mismatch: {relative}")
    return json.loads(payload.decode("utf-8"))


def id_list(value: object, field: str) -> list[int]:
    if (not isinstance(value, list) or any(not isinstance(v, int) or isinstance(v, bool) or v < 1 for v in value)
            or len(set(value)) != len(value)):
        raise ValueError(f"{field} must contain unique positive run IDs")
    return value


def records(value: object, label: str) -> list[dict]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(f"{label} must be an array of objects")
    return value


class RequiredContexts(set):
    def __init__(self, names: set[str], info: dict):
        super().__init__(names)
        self.info = info


def required_info(required: object) -> dict:
    if not isinstance(required, dict):
        return {"complete": False, "contexts": [], "identities": [], "required_workflows": [], "gaps": ["required metadata absent"]}
    names, identities, workflows, gaps = set(), [], [], []
    status = required.get("source_status") if isinstance(required.get("source_status"), dict) else {}
    if "source_status" in required and not isinstance(required["source_status"], dict):
        gaps.append("malformed source completeness metadata")
    if "completeness" in required and (not isinstance(required["completeness"], dict)
                                       or required["completeness"].get("complete") is not True):
        gaps.append("required metadata collection is incomplete")
    classic, rules = required.get("classic_protection"), required.get("rulesets")
    classic_ok = isinstance(classic, dict) and "unavailable" not in classic
    rules_ok = isinstance(rules, list)
    for source, good in (("classic_protection", classic_ok), ("rulesets", rules_ok)):
        source_complete = (isinstance(status.get(source), dict) and status[source].get("complete") is True
                           and status[source].get("state", "complete") == "complete")
        if not good or ("source_status" in required and not source_complete):
            gaps.append(f"{source} incomplete or inaccessible")
    def add(context: object, app: object, source: str) -> None:
        if isinstance(context, str) and context.strip():
            names.add(context)
            if app is not None and (type(app) is not int or app < -1):
                gaps.append(f"{source} has malformed app identity")
                app = UNKNOWN
            identity = {"context": context, "app_id": app, "source": source}
            if identity not in identities:
                identities.append(identity)
        else:
            gaps.append(f"{source} has malformed required context")
    def checks(value: object, source: str) -> None:
        if not isinstance(value, list):
            gaps.append(f"{source} required checks must be an array")
            return
        for check in value:
            if not isinstance(check, dict):
                gaps.append(f"{source} has malformed required check")
            else:
                add(check.get("context"), check.get("integration_id", check.get("app_id")), source)
    if classic_ok:
        if not ({"contexts", "checks"} & set(classic)):
            gaps.append("classic_protection required fields are absent")
        contexts = classic.get("contexts", [])
        if not isinstance(contexts, list):
            gaps.append("classic_protection contexts must be an array")
        else:
            for context in contexts:
                add(context, None, "classic_protection")
        if "checks" in classic:
            checks(classic["checks"], "classic_protection")
    if rules_ok:
        for rule in rules:
            if not isinstance(rule, dict) or not isinstance(rule.get("type"), str) or not rule["type"]:
                gaps.append("malformed ruleset entry")
                continue
            if rule["type"] not in {"required_status_checks", "workflows"}:
                continue
            params = rule.get("parameters")
            if not isinstance(params, dict):
                gaps.append("malformed ruleset parameters")
                if rule["type"] == "workflows":
                    workflows.append(rule)
                continue
            if rule["type"] == "required_status_checks":
                checks(params.get("required_status_checks"), "rulesets")
            if rule.get("type") == "workflows":
                workflows.append(rule)
                declared = params.get("workflows")
                if (not isinstance(declared, list) or any(not isinstance(item, dict)
                    or not isinstance(item.get("path"), str) or not item["path"].strip() for item in declared)):
                    gaps.append("malformed required workflow identities")
    if workflows:
        gaps.append("required workflow identities are retained but not resolved to job contexts")
    return {"branch": required.get("branch", UNKNOWN), "complete": not gaps, "contexts": sorted(names),
            "identities": identities, "required_workflows": workflows, "gaps": list(dict.fromkeys(gaps))}


def required_contexts(required: dict) -> set[str] | None:
    info = required_info(required)
    return RequiredContexts(set(info["contexts"]), info) if info["contexts"] or info["complete"] else None


def source_identity_evidence(manifest: dict | None, meta: dict, runs: list[dict],
                             jobs: dict[int, list[dict]], attempts: dict[int, list[dict]]) -> dict:
    """Check imported fields again; a collector marker is not provider attestation."""
    result = {"state": UNKNOWN, "collector_assessment": UNKNOWN, "independently_verified": False,
              "basis": "Offline record consistency and loaded payload hashes; provider authentication and cryptographic attestation are external.",
              "gaps": []}
    repo = manifest.get("repo") if manifest else meta.get("repo")
    repo_id = meta.get("repository_id")
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        result["gaps"].append("expected repository identity is unavailable")
        return result
    observed = {"state": "verified", "checked_records": {"metadata": 0, "runs": 0, "jobs": 0, "attempts": 0}, "gaps": []}
    repo_info = {"id": repo_id if type(repo_id) is int and repo_id > 0 else None, "full_name": repo}
    try:
        check_source_identity(repo_info, repo, repo_info, observed, "metadata")
        for run in runs:
            rid = run["id"]
            check_source_identity(run, repo, repo_info, observed, "runs", run_id=rid)
            for kind, by_run in (("jobs", jobs), ("attempts", attempts)):
                for record in by_run.get(rid, []):
                    check_source_identity(record, repo, repo_info, observed, kind, expected_run=run, run_id=rid)
                    if kind == "attempts" and any(record.get(field) is not None and run.get(field) is not None
                                                   and record[field] != run[field] for field in ("workflow_id", "event")):
                        raise ValueError("attempt workflow/event identity contradicts selected run")
    except GhError as exc:
        raise ValueError(f"snapshot source identity conflict: {exc.kind}") from exc
    result["record_consistency"] = observed
    freshness = manifest.get("freshness") if manifest else None
    marker = freshness.get("source_identity") if isinstance(freshness, dict) else None
    meta_freshness = meta.get("freshness")
    meta_marker = meta_freshness.get("source_identity") if isinstance(meta_freshness, dict) else None
    hashed = manifest is not None and isinstance(manifest.get("files"), dict)
    result["loaded_payload_hashes_verified"] = hashed
    if not hashed:
        result["gaps"].append("legacy or unhashed input cannot verify the collector source assessment")
        return result
    if (not isinstance(marker, dict) or marker.get("schema") != "ci-source-identity/1"
            or marker.get("state") not in {"verified", UNKNOWN} or not isinstance(marker.get("gaps"), list)
            or not isinstance(marker.get("checked_records"), dict)
            or any(type(marker["checked_records"].get(key)) is not int or marker["checked_records"][key] < 0
                   for key in ("metadata", "runs", "jobs", "attempts"))):
        result["gaps"].append("collector source assessment is absent or malformed")
        return result
    if (marker != meta_marker or not isinstance(marker.get("expected_repository"), str)
            or marker["expected_repository"].casefold() != repo.casefold()
            or marker.get("expected_repository_id") != (repo_id if type(repo_id) is int and repo_id > 0 else UNKNOWN)):
        raise ValueError("collector source assessment contradicts snapshot metadata")
    result["collector_assessment"] = marker
    if marker["state"] == "verified" and not marker["gaps"] and observed["state"] == "verified":
        result["state"] = "collector_verified"
    else:
        result["gaps"].append("source binding has missing identities or incomplete payloads")
    return result


def load(input_dir: Path) -> tuple[list[dict], dict[int, list[dict]], dict, set[str] | None]:
    """Read a published manifest only; legacy input never globs unrelated payloads."""
    root, manifest, pointer = input_dir.resolve(), None, None
    gaps = []
    if (root / "current.json").exists():
        pointer = read_json(root, "current.json")
        if not isinstance(pointer, dict) or pointer.get("schema") != "ci-snapshot-pointer/1":
            raise ValueError("invalid current snapshot pointer")
        sid = pointer.get("snapshot_id")
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]*", sid):
            raise ValueError("invalid snapshot identity")
        expected = f"snapshots/{sid}"
        if pointer.get("snapshot_path") != expected:
            raise ValueError("snapshot pointer path does not match identity")
        if not isinstance(pointer.get("repo"), str) or not pointer["repo"]:
            raise ValueError("snapshot repository identity is absent")
        target = (root / expected).resolve()
        target.relative_to(root)
        root = target
        manifest = read_json(root, "raw/manifest.json")
        if (not isinstance(manifest, dict) or manifest.get("schema") != "ci-snapshot-manifest/1"
                or manifest.get("snapshot_id") != sid or manifest.get("repo") != pointer.get("repo")):
            raise ValueError("snapshot manifest does not match pointer")
    else:
        gaps.append({"code": "legacy_freshness_unverified", "detail": "raw payload freshness cannot be established; no stale-file inference"})
    def payload(relative: str):
        expected = NO_INTEGRITY_EXPECTATION
        if manifest is not None and "files" in manifest:
            inventory = manifest["files"]
            if not isinstance(inventory, dict) or relative not in inventory:
                raise ValueError(f"snapshot payload is absent from integrity inventory: {relative}")
            expected = inventory[relative]
        return read_json(root, f"raw/{relative}", expected)
    if manifest is not None and "files" not in manifest:
        gaps.append({"code": "snapshot_integrity_unverified", "detail": "manifest has no payload hashes"})
    runs = records(payload("runs.json"), "runs")
    selected = id_list([r.get("id") for r in runs], "runs")
    if manifest is not None and set(id_list(manifest.get("selected_run_ids"), "selected_run_ids")) != set(selected):
        raise ValueError("manifest run membership does not match runs.json")
    meta_path = root / "raw/collect_meta.json"
    meta = payload("collect_meta.json") if meta_path.exists() else {}
    if not isinstance(meta, dict):
        raise ValueError("collection metadata must be an object")
    if manifest is not None and meta.get("repo", manifest["repo"]) != manifest["repo"]:
        raise ValueError("collection metadata repository does not match manifest")
    jobs, attempts = {}, {}
    for kind, destination in (("jobs", jobs), ("attempts", attempts)):
        permitted = id_list(manifest.get(f"{kind}_payload_run_ids", []), f"{kind}_payload_run_ids") if manifest else selected
        if not set(permitted) <= set(selected):
            raise ValueError(f"manifest {kind} includes unselected run IDs")
        for rid in permitted:
            relative = f"raw/{kind}/{rid}.json"
            if not (root / relative).exists():
                if manifest is not None:
                    gaps.append({"code": "missing_payload", "kind": kind, "run_id": rid})
                continue
            rows = records(payload(f"{kind}/{rid}.json"), relative)
            if any(row.get("run_id", rid) != rid or (kind == "attempts" and row.get("id", rid) != rid) for row in rows):
                raise ValueError(f"{relative} contains a different run identity")
            destination[rid] = rows
    req = None
    if (manifest is None or manifest.get("required_checks_payload") is True) and (root / "raw/required_checks.json").exists():
        req = payload("required_checks.json")
    info = required_info(req)
    source_identity = source_identity_evidence(manifest, meta, runs, jobs, attempts)
    meta = {**meta, "_attempts_by_run": attempts, "_required_state": info, "_load_gaps": gaps,
            "_source_identity": source_identity,
            "freshness": "published_snapshot" if manifest else "unverified_legacy"}
    if manifest:
        meta.update(repo=manifest["repo"], snapshot_id=manifest["snapshot_id"], _manifest=manifest)
    return runs, jobs, meta, required_contexts(req) if req is not None else None


def attempt_number(row: dict) -> int | None:
    value = row.get("run_attempt")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def job_identity(job: dict) -> str:
    return str(job.get("logical_job_id") or job.get("name") or "?")


def effective_jobs(run: dict, jobs: list[dict]) -> tuple[list[dict], list[str]]:
    """Latest observed version of each job, retaining successful partial-rerun predecessors."""
    candidates = defaultdict(list)
    gaps = []
    run_number = attempt_number(run)
    if run_number is None:
        return [], ["run attempt identity absent or invalid"]
    for job in jobs:
        job_number = attempt_number(job)
        if job_number is None:
            gaps.append("job attempt identity absent or invalid")
        elif job_number <= run_number:
            candidates[job_identity(job)].append(job)
        else:
            gaps.append("job belongs to a newer attempt than the captured run")
    chosen = []
    for key, group in sorted(candidates.items()):
        latest = max(attempt_number(j) for j in group)
        group = [j for j in group if attempt_number(j) == latest]
        unique = {json.dumps(j, sort_keys=True): j for j in group}
        group = list(unique.values())
        if key == "?" or len(group) > 1:
            gaps.append(f"ambiguous logical job identity: {key}")
        if latest < run_number:
            group = [j for j in group if j.get("conclusion") == "success"]
            if not group:
                gaps.append(f"non-successful predecessor not rerun: {key}")
        chosen.extend(group)
    return chosen, gaps


def latest_attempt(run: dict, jobs: list[dict]) -> list[dict]:
    return effective_jobs(run, jobs)[0]


def run_end(run: dict, jobs: list[dict]) -> str | None:
    ends = [j.get("completed_at") for j in latest_attempt(run, jobs) if ts(j.get("completed_at"))]
    return max(ends, key=ts) if ends else None


def union_seconds(intervals: list[tuple[datetime, datetime]]) -> float:
    total, end = 0.0, None
    for start, finish in sorted(intervals):
        if end is None or start > end:
            total += (finish - start).total_seconds()
        elif finish > end:
            total += (finish - end).total_seconds()
        end = max(end, finish) if end is not None else finish
    return total


def step_metrics(job: dict) -> dict:
    start, end = ts(job.get("started_at")), ts(job.get("completed_at"))
    work, setup, intervals, setup_intervals, per_step = [], [], [], [], []
    missing = 0
    for step in job.get("steps") or []:
        if not isinstance(step, dict):
            missing += 1
            continue
        duration = secs(step.get("started_at"), step.get("completed_at"))
        if duration is None:
            missing += 1
            continue
        name = str(step.get("name", "?"))
        work.append(duration)
        is_setup = bool(SETUP_STEP.match(name))
        if is_setup:
            setup.append(duration)
        per_step.append((name, duration))
        if start is not None and end is not None and end >= start:
            left, right = max(start, ts(step["started_at"])), min(end, ts(step["completed_at"]))
            if right >= left:
                intervals.append((left, right))
                if is_setup:
                    setup_intervals.append((left, right))
    total = sum(work) if work else None
    wall = secs(job.get("started_at"), job.get("completed_at"))
    coverage = union_seconds(intervals) if intervals else None
    setup_coverage = union_seconds(setup_intervals) if intervals else None
    return {"step_work_s": total, "setup_work_share": sum(setup) / total if total else None,
            "step_wall_coverage_s": coverage, "setup_wall_coverage_s": setup_coverage,
            "setup_wall_share": setup_coverage / wall if wall and setup_coverage is not None else None,
            "execution_excluding_setup_coverage_s": coverage - setup_coverage if coverage is not None else None,
            "missing_step_timings": missing, "per_step": per_step}


PHASE_METRICS = ("waiting_s", "setup_s", "running_s", "unattributed_s", "overlap_s", "runner_s")
PHASE_CLASSIFICATION = (
    "Preliminary step-name heuristic, not command instrumentation: existing SETUP_STEP names "
    "include checkout, installs, caches and lifecycle/post steps; other named steps "
    "are running. Waiting excludes dependency/approval delay before job creation. "
    "Unattributed time is runner wall time outside all observed step intervals."
)
MAPPED_CLASSIFICATION = (
    "Reviewed workflow/config/job/step-number map, not independent authentication: "
    "setup is runner start to first mapped work start; running is mapped work union. "
    "Restore, save and teardown step unions remain separate in mapped_phase_s. "
    "Waiting excludes dependency/approval delay before job creation."
)


def phase_observation(run: dict, job: dict, phase_map: reviewed_phases.PhaseMap | None = None) -> dict:
    """Keep incomplete observations out of complete phase latency distributions."""
    result = {key: None for key in PHASE_METRICS}
    number, latest = attempt_number(job), attempt_number(run)
    outcome = job.get("conclusion")
    result.update(attempt=number, outcome=outcome if isinstance(outcome, str) and outcome else UNKNOWN,
                  gaps=[], phase_state=UNKNOWN,
                  classification="preliminary_heuristic" if phase_map is None else UNKNOWN,
                  mapped_phase_s={phase: None for phase in reviewed_phases.PHASES})
    gaps = result["gaps"]
    if number is None or latest is None or number > latest:
        gaps.append("missing or inconsistent attempt identity")
    name = job.get("logical_job_id") or job.get("name")
    if not isinstance(name, str) or not name.strip():
        gaps.append("job identity absent or malformed")
    if job.get("status") != "completed" or outcome not in ("success", "failure"):
        gaps.append("job is not a completed success/failure observation")
    if gaps:
        return result
    created, start, end = (ts(job.get(key)) for key in ("created_at", "started_at", "completed_at"))
    if start is None or end is None or end < start:
        gaps.append("missing or invalid runner interval")
        return result
    result["runner_s"] = (end - start).total_seconds()
    if created is None or created > start:
        gaps.append("missing or invalid waiting interval")
    else:
        result["waiting_s"] = (start - created).total_seconds()
    steps = job.get("steps")
    if not isinstance(steps, list) or not steps:
        gaps.append("step inventory absent or empty")
        return result
    entry = None
    if phase_map is not None:
        entry, gap = reviewed_phases.select(phase_map, run, job)
        if gap:
            gaps.append(gap)
            return result
    setup, running, by_number = [], [], {}
    step_gaps = []
    for step in steps:
        if not isinstance(step, dict):
            step_gaps.append("malformed step record")
            continue
        name = step.get("name")
        left, right = ts(step.get("started_at")), ts(step.get("completed_at"))
        if not isinstance(name, str) or not name.strip():
            step_gaps.append("step name absent or malformed")
        elif step.get("status") != "completed" or step.get("conclusion") not in ("success", "failure"):
            step_gaps.append("step is not a completed success/failure observation")
        elif outcome == "success" and step.get("conclusion") == "failure":
            step_gaps.append("successful job contains a failed step")
        elif left is None or right is None or not start <= left <= right <= end:
            step_gaps.append("step timing missing, invalid or outside runner interval")
        else:
            if entry is None:
                (setup if SETUP_STEP.match(name) else running).append((left, right))
            else:
                step_number = step.get("number")
                if not reviewed_phases.positive(step_number) or step_number in by_number:
                    step_gaps.append("mapped step number is missing, malformed or duplicated")
                elif by_number and step_number <= max(by_number):
                    step_gaps.append("mapped step numbers are not in execution order")
                else:
                    by_number[step_number] = (left, right)
    if step_gaps:
        gaps.extend(sorted(set(step_gaps)))
        return result
    if entry is not None:
        if set(by_number) != set(entry["steps"]):
            gaps.append("reviewed phase-map does not match the complete step inventory")
            return result
        phases = {phase: [by_number[n] for n, p in entry["steps"].items() if p == phase]
                  for phase in reviewed_phases.PHASES}
        first_work = by_number[min(n for n, p in entry["steps"].items() if p == "work")][0]
        last_work = max(right for _, right in phases["work"])
        if (any(right > first_work for left, right in phases["setup"] + phases["restore"])
                or any(left < first_work for left, right in phases["work"])
                or any(left < last_work for left, right in phases["save"] + phases["teardown"])):
            gaps.append("reviewed phase-map boundaries conflict with the observed timeline")
            return result
        setup_s = (first_work - start).total_seconds()
        details = {phase: union_seconds(intervals) for phase, intervals in phases.items()}
        # Setup is a boundary interval, including pre-work gaps, like the budget
        # gate. Post-work steps still consume runner time and cannot disappear.
        intervals = [(start, first_work)] + phases["work"] + phases["save"] + phases["teardown"]
        covered = union_seconds(intervals)
        result.update(setup_s=setup_s, running_s=details["work"], mapped_phase_s=details,
                      overlap_s=max(0.0, setup_s + details["work"] + details["save"] + details["teardown"] - covered),
                      unattributed_s=max(0.0, result["runner_s"] - covered), classification="reviewed_map")
        if not gaps:
            result["phase_state"] = "complete"
        return result
    setup_s, running_s = union_seconds(setup), union_seconds(running)
    covered_s = union_seconds(setup + running)
    result.update(setup_s=setup_s, running_s=running_s,
                  overlap_s=max(0.0, setup_s + running_s - covered_s),
                  unattributed_s=max(0.0, result["runner_s"] - covered_s))
    if not gaps:
        result["phase_state"] = "complete"
    return result


def phase_summary(observations: list[dict]) -> dict:
    """Count exclusions alongside every distribution; zero observations is unknown."""
    count = len(observations)
    complete = sum(row["phase_state"] == "complete" for row in observations)
    return {"observations": count, "complete_phase_observations": complete,
            "unknown_phase_observations": count - complete,
            "outcomes": dict(sorted(Counter(row["outcome"] for row in observations).items())),
            "attempts": dict(sorted(Counter(str(row["attempt"]) if row["attempt"] is not None else UNKNOWN
                                            for row in observations).items())),
            **{key: dist([row[key] for row in observations]) for key in PHASE_METRICS},
            "unknown_by_metric": {key: sum(row[key] is None for row in observations) for key in PHASE_METRICS},
            "classification_counts": dict(sorted(Counter(row["classification"] for row in observations).items())),
            "mapped_phase_s": {phase: dist([row["mapped_phase_s"][phase] for row in observations])
                               for phase in reviewed_phases.PHASES},
            "unknown_by_mapped_phase": {phase: sum(row["mapped_phase_s"][phase] is None for row in observations)
                                        for phase in reviewed_phases.PHASES},
            "gaps": dict(sorted(Counter(gap for row in observations for gap in row["gaps"]).items()))}


def job_phase_metrics(runs: list[dict], jobs_by_run: dict[int, list[dict]], phase_map: reviewed_phases.PhaseMap | None = None) -> dict:
    """Retain failed first attempts instead of replacing them with successful reruns."""
    by_job = defaultdict(list)
    for run in runs:
        # API pages may repeat identical records. Count each distinct payload once
        # within its run; conflicting same-ID records are unusable evidence, and
        # unlike effective_jobs(), older attempts are never discarded.
        seen = set()
        records = jobs_by_run.get(run["id"], [])
        id_records = defaultdict(set)
        mapped_records = defaultdict(set)
        for job in records:
            payload = json.dumps(job, sort_keys=True)
            if job.get("id") is not None:
                id_records[str(job["id"])].add(payload)
            if phase_map is not None:
                entry, _ = reviewed_phases.select(phase_map, run, job)
                if entry is not None:
                    key = (entry["job_identity"]["field"], entry["job_identity"]["value"], attempt_number(job))
                    mapped_records[key].add(payload)
        for job in records:
            payload = json.dumps(job, sort_keys=True)
            if payload in seen:
                continue
            seen.add(payload)
            observation = phase_observation(run, job, phase_map)
            if phase_map is not None:
                entry, _ = reviewed_phases.select(phase_map, run, job)
                if entry is not None:
                    key = (entry["job_identity"]["field"], entry["job_identity"]["value"], attempt_number(job))
                    if len(mapped_records[key]) > 1:
                        observation.update({metric: None for metric in PHASE_METRICS if metric not in ("waiting_s", "runner_s")})
                        observation.update(phase_state=UNKNOWN, classification=UNKNOWN,
                                           mapped_phase_s={phase: None for phase in reviewed_phases.PHASES})
                        observation["gaps"].append("reviewed phase-map job identity matches multiple records in one attempt")
            if job.get("id") is not None and len(id_records[str(job["id"])]) > 1:
                observation.update({key: None for key in PHASE_METRICS})
                observation["mapped_phase_s"] = {phase: None for phase in reviewed_phases.PHASES}
                observation["classification"] = UNKNOWN
                observation["phase_state"] = UNKNOWN
                observation["gaps"].append("conflicting records for one job ID")
            by_job[job_identity(job)].append(observation)
    result = {}
    for name, observations in sorted(by_job.items()):
        result[name] = {**phase_summary(observations), "classification": PHASE_CLASSIFICATION if phase_map is None else MAPPED_CLASSIFICATION,
                        "identity_basis": "logical_job_id when supplied, otherwise job name; exact duplicates removed within each run; conflicting same-ID payloads count as separate invalid observations",
                        "decomposition": ("runner_s = setup_s + running_s - overlap_s + unattributed_s" if phase_map is None else
                                          "runner_s = setup_s + running_s + mapped_phase_s.save + mapped_phase_s.teardown - overlap_s + unattributed_s")
                                         + " for each complete observation; percentiles are not additive",
                        "first_attempt": phase_summary([row for row in observations if row["attempt"] == 1]),
                        "reruns": phase_summary([row for row in observations if row["attempt"] is not None and row["attempt"] > 1]),
                        "unknown_attempt": phase_summary([row for row in observations if row["attempt"] is None]),
                        "by_outcome": {outcome: phase_summary([row for row in observations if row["outcome"] == outcome])
                                       for outcome in sorted({row["outcome"] for row in observations})}}
    return result


def analyze_group(runs: list[dict], jobs_by_run: dict[int, list[dict]], required: set[str] | None = None,
                  phase_map: reviewed_phases.PhaseMap | None = None) -> dict:
    metrics = {key: [] for key in ("job_queue_s", "setup_work_share", "step_work_s", "step_wall_coverage_s",
                                  "setup_wall_coverage_s", "setup_wall_share", "execution_excluding_setup_coverage_s")}
    durations, per_step, slowest = defaultdict(list), defaultdict(list), Counter()
    wall, gaps, skew, identity_gaps = [], [], [], []
    missing_steps = 0
    required_complete = required is not None and getattr(required, "info", {"complete": True})["complete"]
    for run in runs:
        jobs, ambiguity = effective_jobs(run, jobs_by_run.get(run["id"], []))
        identity_gaps.extend({"run_id": run["id"], "detail": gap} for gap in ambiguity)
        success = run.get("conclusion") == "success"
        if success:
            wall.append(secs(run.get("created_at"), run_end(run, jobs)))
        ends, families = [], defaultdict(list)
        for job in jobs:
            metrics["job_queue_s"].append(secs(job.get("created_at"), job.get("started_at")))
            if job.get("conclusion") == "skipped":
                continue
            duration = secs(job.get("started_at"), job.get("completed_at"))
            if duration is None:
                continue
            name = str(job.get("name", "?"))
            if success:
                durations[name].append(duration)
                ends.append((ts(job["completed_at"]), name))
                match = MATRIX.match(name)
                if match:
                    families[match.group(1)].append(duration)
            phases = step_metrics(job)
            for key in metrics:
                if key != "job_queue_s":
                    metrics[key].append(phases[key])
            missing_steps += phases["missing_step_timings"]
            for step, seconds in phases["per_step"]:
                per_step[f"{name} / {step}"].append(seconds)
        ends.sort()
        if ends:
            slowest[ends[-1][1]] += 1
        if len(ends) > 1:
            gaps.append((ends[-1][0] - ends[-2][0]).total_seconds())
        for times in families.values():
            if len(times) > 1 and statistics.median(times) > 0:
                skew.append(max(times) / statistics.median(times))
    observed_attempt_numbers = [number for run in runs if (number := attempt_number(run)) is not None]
    result = {"runs": len(runs), "conclusions": dict(Counter(r.get("conclusion") or UNKNOWN for r in runs)),
              "run_wall_s_success": dist(wall), **{key: dist(values) for key, values in metrics.items()},
              "shard_skew": dist(skew), "rerun_rate": ratio(sum(number > 1 for number in observed_attempt_numbers), len(observed_attempt_numbers)),
              "missing_run_attempt_identity": len(runs) - len(observed_attempt_numbers),
              "last_finishing_job": {name: {"runs": count, "required": True if required is not None and name in required else False if required_complete else UNKNOWN,
                                               "match_basis": "context name only"} for name, count in slowest.most_common()} or UNKNOWN,
              "gap_to_runner_up_s": dist(gaps), "job_duration_s": {name: dist(v) for name, v in sorted(durations.items())},
              "step_duration_s": {name: dist(v) for name, v in sorted(per_step.items())},
              "job_phases": job_phase_metrics(runs, jobs_by_run, phase_map),
              "jobs_missing_for_runs": sum(r["id"] not in jobs_by_run for r in runs),
              "missing_step_timings": missing_steps, "identity_gaps": identity_gaps}
    result["setup_share"] = result["setup_work_share"]
    return result


def run_context(run: dict) -> tuple:
    prs = run.get("pull_requests") or []
    pr_context = tuple(sorted((str(pr.get("number", "?")), str((pr.get("base") or {}).get("ref", "?"))) for pr in prs if isinstance(pr, dict)))
    return (run.get("head_sha"), run.get("event"), run.get("head_branch", UNKNOWN), pr_context)


def workflow_identity(run: dict) -> str:
    return str(run.get("workflow_id") or run.get("path") or run.get("name") or UNKNOWN)


def dedupe_runs(runs: list[dict]) -> list[dict]:
    by_id = defaultdict(list)
    minimum = datetime.min.replace(tzinfo=timezone.utc)
    for run in runs:
        by_id[run["id"]].append(run)
    out = []
    for key in sorted(by_id):
        group = by_id[key]
        latest = max(group, key=lambda row: (attempt_number(row) or 0, ts(row.get("updated_at")) or minimum,
                                             json.dumps(row, sort_keys=True)))
        conflict = any(len({json.dumps(row[field], sort_keys=True) for row in group if row.get(field) is not None}) > 1
                       for field in ("head_sha", "workflow_id", "event", "head_branch", "created_at"))
        for number in {attempt_number(row) for row in group}:
            conclusions = {row.get("conclusion") for row in group if attempt_number(row) == number
                           and row.get("conclusion") is not None}
            conflict = conflict or len(conclusions) > 1
        if conflict:
            latest = {**latest, "conclusion": None, "status": UNKNOWN, "_identity_conflict": True}
        if any(attempt_number(row) is None for row in group):
            latest = {**latest, "run_attempt": None, "_attempt_identity_unknown": True}
        out.append(latest)
    return out


def push_to_green(runs: list[dict], jobs_by_run: dict[int, list[dict]]) -> dict | str:
    groups = defaultdict(list)
    for run in dedupe_runs(runs):
        if run.get("event") in {"push", "pull_request", "merge_group"} and run.get("head_sha"):
            groups[run_context(run)].append(run)
    values = []
    minimum = datetime.min.replace(tzinfo=timezone.utc)
    for group in groups.values():
        latest = {}
        for run in group:
            key = workflow_identity(run)
            rank = (ts(run.get("created_at")) or minimum, run["id"])
            if key not in latest or rank > latest[key][0]:
                latest[key] = (rank, run)
        endpoints = []
        for _, run in latest.values():
            jobs, ambiguity = effective_jobs(run, jobs_by_run.get(run["id"], []))
            if (run.get("conclusion") != "success" or run.get("status", "completed") != "completed" or not jobs or ambiguity
                    or any(j.get("conclusion") != "success" or secs(j.get("started_at"), j.get("completed_at")) is None for j in jobs)):
                break
            endpoints.append(ts(run_end(run, jobs)))
        else:
            starts = [ts(r.get("created_at")) for r in group]
            if endpoints and all(starts) and all(endpoints):
                delta = (max(endpoints) - min(starts)).total_seconds()
                if delta >= 0:
                    values.append(delta)
    return dist(values)


def attempt_history(run: dict, attempts: dict[int, list[dict]] | None) -> list[dict]:
    by_number = {}
    run_number = attempt_number(run)
    minimum = datetime.min.replace(tzinfo=timezone.utc)
    for record in (attempts or {}).get(run["id"], []):
        number = attempt_number(record)
        if number is not None and (run_number is None or number <= run_number):
            rank = ts(record.get("updated_at")) or ts(record.get("run_started_at")) or minimum
            previous = by_number.get(number)
            previous_rank = (ts(previous.get("updated_at")) or ts(previous.get("run_started_at")) or minimum) if previous else None
            if previous is None or rank > previous_rank:
                by_number[number] = record
            elif rank == previous_rank and record.get("conclusion") != previous.get("conclusion"):
                by_number[number] = {**previous, "conclusion": None, "_conflicting_attempt_evidence": True}
    if run_number is not None:
        by_number.setdefault(run_number, run)
    return [by_number[number] for number in sorted(by_number)]


def first_pass(runs: list[dict], attempts: dict[int, list[dict]] | None = None) -> dict | str:
    outcomes = [next((a.get("conclusion") for a in attempt_history(run, attempts) if attempt_number(a) == 1), None) for run in runs]
    known = [value for value in outcomes if value == "success" or value in FAILURES]
    return ratio(sum(value in FAILURES for value in known), len(known))


def flaky(runs: list[dict], attempts: dict[int, list[dict]] | None = None) -> dict | str:
    histories = [attempt_history(run, attempts) for run in runs]
    comparable = [history for history in histories if len(history) > 1 and all(a.get("conclusion") == "success" or a.get("conclusion") in FAILURES for a in history)]
    recovered = sum(any(a.get("conclusion") in FAILURES and any(b.get("conclusion") == "success" for b in history[i + 1:]) for i, a in enumerate(history)) for history in comparable)
    return ratio(recovered, len(comparable))


def reliability(runs: list[dict], jobs: dict[int, list[dict]], attempts: dict[int, list[dict]]) -> dict:
    first_outcomes, execution, recovery, retry_wait = Counter(), [], [], []
    missing, intentional = 0, 0
    for run in runs:
        history = attempt_history(run, attempts)
        first = next((a for a in history if attempt_number(a) == 1), None)
        if first is None:
            missing += 1
        else:
            first_outcomes[first.get("conclusion") or UNKNOWN] += 1
        ends, starts = {}, {}
        for record in history:
            number = attempt_number(record)
            these_jobs = [j for j in jobs.get(run["id"], []) if attempt_number(j) == number]
            valid_ends = [ts(j.get("completed_at")) for j in these_jobs if ts(j.get("completed_at"))]
            valid_starts = [ts(j.get("started_at")) for j in these_jobs if ts(j.get("started_at"))]
            start = ts(record.get("run_started_at")) or (min(valid_starts) if valid_starts else None)
            end = ts(record.get("completed_at")) or (max(valid_ends) if valid_ends else None)
            if start and end and end >= start:
                execution.append((end - start).total_seconds())
                starts[number], ends[number] = start, end
        for number in sorted(starts):
            if number - 1 in ends and starts[number] >= ends[number - 1]:
                retry_wait.append((starts[number] - ends[number - 1]).total_seconds())
        if len(history) > 1 and all(a.get("conclusion") == "success" for a in history):
            intentional += 1  # Successful-repeat observation, not proof of the actor's intent.
        if first and first.get("conclusion") in FAILURES:
            successes = [attempt_number(a) for a in history if a.get("conclusion") == "success"]
            if successes:
                success = min(successes)
                complete_prefix = set(range(1, success + 1)) <= {attempt_number(a) for a in history}
                if complete_prefix and 1 in ends and success in ends and ends[success] >= ends[1]:
                    recovery.append((ends[success] - ends[1]).total_seconds())
    return {"first_attempt_outcomes": dict(first_outcomes) or UNKNOWN, "missing_first_attempt_history": missing,
            "first_pass_failure_rate": first_pass(runs, attempts), "suspected_retry_recovery_rate": flaky(runs, attempts),
            "successful_repeat_runs": intentional, "attempt_execution_s": dist(execution),
            "observed_retry_wait_s": dist(retry_wait), "first_failure_to_success_s": dist(recovery),
            "diagnosis": "failure-to-success is a recovery observation; investigate before calling it flakiness"}


def unique_jobs(jobs_by_run: dict[int, list[dict]]):
    seen = set()
    for rid, jobs in sorted(jobs_by_run.items()):
        for job in jobs:
            key = (rid, job.get("id")) if job.get("id") is not None else (rid, json.dumps(job, sort_keys=True))
            if key not in seen:
                seen.add(key)
                yield rid, job


def labels(job: dict) -> str:
    return ",".join(sorted(str(label) for label in job.get("labels") or [])) or UNKNOWN


def usage(runs: list[dict], jobs_by_run: dict[int, list[dict]]) -> dict:
    seconds, rounded, counts = Counter(), Counter(), Counter()
    cancelled_runs = {r["id"] for r in runs if r.get("conclusion") == "cancelled"}
    cancelled_run_seconds, cancelled_job_seconds, missing = 0.0, 0.0, 0
    for rid, job in unique_jobs(jobs_by_run):
        if job.get("conclusion") == "skipped":
            continue
        duration = secs(job.get("started_at"), job.get("completed_at"))
        if duration is None:
            missing += 1
            continue
        label = labels(job)
        seconds[label] += duration
        rounded[label] += math.ceil(duration / 60)
        counts[label] += 1
        if rid in cancelled_runs:
            cancelled_run_seconds += duration
        if job.get("conclusion") == "cancelled":
            cancelled_job_seconds += duration
    return {"runner_seconds_by_label": dict(seconds) or UNKNOWN,
            "rounded_runner_minutes_estimate_by_label": dict(rounded) or UNKNOWN,
            "observed_jobs_by_label": dict(counts) or UNKNOWN, "missing_job_durations": missing,
            "cancelled_run_runner_seconds": cancelled_run_seconds if counts else UNKNOWN,
            "cancelled_job_runner_seconds": cancelled_job_seconds if counts else UNKNOWN,
            "complete": missing == 0 and all(r["id"] in jobs_by_run for r in runs) and bool(runs)}


def billable(jobs_by_run: dict[int, list[dict]]) -> dict | str:
    return usage([], jobs_by_run)["rounded_runner_minutes_estimate_by_label"]


def costs(jobs: dict[int, list[dict]], pricing: object, complete: bool, selected_ids: set[int]) -> dict:
    out = {"status": UNKNOWN, "observed_runner_cost_estimate": UNKNOWN, "cost_per_merge_estimate": UNKNOWN,
           "gaps": [], "scope": "selected observed jobs only; storage, transfer, allowances and fixed costs excluded"}
    if pricing is None:
        out["gaps"].append("pricing inputs absent")
        return out
    if (not isinstance(pricing, dict) or pricing.get("schema") != "ci-pricing/1"
            or not isinstance(pricing.get("currency"), str) or not pricing["currency"].strip()
            or not isinstance(pricing.get("source"), str) or not pricing["source"].strip()
            or not ts(str(pricing.get("as_of", "")) + "T00:00:00Z") or not isinstance(pricing.get("rates"), list)):
        out["gaps"].append("invalid dated pricing schema/source/currency/rates")
        return out
    rates = {}
    for rate in pricing["rates"]:
        if (not isinstance(rate, dict) or not isinstance(rate.get("labels"), list) or not rate["labels"]
                or any(not isinstance(x, str) or not x for x in rate["labels"])
                or not finite(rate.get("per_minute")) or rate["per_minute"] < 0
                or not finite(rate.get("billing_quantum_seconds", 60)) or rate.get("billing_quantum_seconds", 60) <= 0):
            out["gaps"].append("invalid pricing rate")
            return out
        label = ",".join(sorted(rate["labels"]))
        if label in rates:
            out["gaps"].append("duplicate pricing label set")
            return out
        rates[label] = rate
    total, count = 0.0, 0
    for _, job in unique_jobs(jobs):
        if job.get("conclusion") == "skipped":
            continue
        duration, rate = secs(job.get("started_at"), job.get("completed_at")), rates.get(labels(job))
        if duration is None or rate is None:
            out["gaps"].append("unpriced label set or missing duration")
            continue
        quantum = rate.get("billing_quantum_seconds", 60)
        total += math.ceil(duration / quantum) * quantum / 60 * rate["per_minute"]
        count += 1
    out.update(currency=pricing["currency"], as_of=pricing["as_of"], source=pricing["source"], priced_jobs=count)
    if count and not out["gaps"]:
        out.update(status="estimate", observed_runner_cost_estimate=round(total, 6))
        merged = pricing.get("merged_prs")
        declared_scope = pricing.get("run_ids")
        matched_scope = (isinstance(declared_scope, list)
                         and all(isinstance(rid, int) and not isinstance(rid, bool) for rid in declared_scope)
                         and set(declared_scope) == selected_ids)
        if complete and matched_scope and pricing.get("population_complete") is True and isinstance(merged, int) and not isinstance(merged, bool) and merged > 0:
            out["cost_per_merge_estimate"] = round(total / merged, 6)
            out["denominator_basis"] = "caller-attested matched complete population"
    return out


def apply_cohort_context(runs: list[dict], context: object) -> list[dict]:
    if context is None:
        return runs
    if not isinstance(context, dict) or context.get("schema") != "ci-cohort-context/1" or not isinstance(context.get("runs"), dict):
        raise ValueError("invalid cohort context schema")
    annotations = context["runs"]
    for key, annotation in annotations.items():
        if not isinstance(key, str) or not key.isdigit() or not isinstance(annotation, dict) or set(annotation) - set(COHORT_FIELDS):
            raise ValueError("cohort context requires run-ID keys and supported attributes")
        if any(not isinstance(value, str) or not value.strip() for value in annotation.values()):
            raise ValueError("cohort context attributes must be non-empty strings")
    return [{**run, "cohort": {**(run.get("cohort") if isinstance(run.get("cohort"), dict) else {}), **annotations.get(str(run["id"]), {})}} for run in runs]


def cohort_identity(run: dict, jobs: list[dict]) -> dict:
    annotation = run.get("cohort") if isinstance(run.get("cohort"), dict) else {}
    identity = {"workflow": workflow_identity(run), "event": run.get("event", UNKNOWN), "branch": run.get("head_branch", UNKNOWN),
                "runner_labels": sorted({labels(job) for job in jobs}) or UNKNOWN}
    identity.update({key: annotation.get(key) if isinstance(annotation.get(key), str) and annotation[key] else UNKNOWN for key in COHORT_FIELDS})
    return identity


def analyze(runs: list[dict], jobs_by_run: dict[int, list[dict]], meta: dict,
            required: set[str] | None = None, *, graph: object = None, cohort_context: object = None, pricing: object = None,
            phase_map: object = None) -> dict:
    phase_map = reviewed_phases.validate(phase_map)
    runs = dedupe_runs(apply_cohort_context(runs, cohort_context))
    since = ts(meta.get("since"))
    if since:
        runs = [r for r in runs if ts(r.get("created_at")) and ts(r["created_at"]) >= since]
    selected = {r["id"] for r in runs}
    jobs_by_run = {rid: payload for rid, payload in jobs_by_run.items() if rid in selected}
    attempts = {int(rid): payload for rid, payload in meta.get("_attempts_by_run", {}).items() if int(rid) in selected}
    groups, cohorts = defaultdict(list), defaultdict(list)
    name_ids = defaultdict(set)
    for run in runs:
        name_ids[(run.get("name", "?"), run.get("event", "?"))].add(workflow_identity(run))
    for run in runs:
        name, event = run.get("name", "?"), run.get("event", "?")
        label = f"{name} [{event}]" + (f" (workflow {workflow_identity(run)})" if len(name_ids[(name, event)]) > 1 else "")
        groups[label].append(run)
        identity = cohort_identity(run, jobs_by_run.get(run["id"], []))
        cohorts[json.dumps(identity, sort_keys=True)].append(run)
    created = sorted((r["created_at"] for r in runs if ts(r.get("created_at"))), key=ts)
    required_state = meta.get("_required_state") or getattr(required, "info", None) or {
        "contexts": sorted(required) if required is not None else [], "complete": required is not None,
        "identities": [], "gaps": [] if required is not None else ["required metadata absent"]}
    if required is not None:
        required = RequiredContexts(set(required), required_state)
    physical = usage(runs, jobs_by_run)
    retry = reliability(runs, jobs_by_run, attempts)
    price = costs(jobs_by_run, pricing, physical["complete"], selected)
    return {"schema": "ci-metrics/2", "purpose": "diagnostic proxies, not gate certification",
            "phase_mapping": {"status": "reviewed_input", "sha256": phase_map.sha256,
                              "review_reference": phase_map.review_reference, "independently_verified": False}
                             if phase_map is not None else {"status": "preliminary_heuristic", "independently_verified": False},
            "collection_scope": meta.get("collection_scope", {"name": UNKNOWN}),
            "source": meta.get("source", "collect_gh.py raw/"), "repo": meta.get("repo", UNKNOWN),
            "collected_at": meta.get("collected_at", UNKNOWN), "snapshot_id": meta.get("snapshot_id", UNKNOWN),
            "freshness": meta.get("freshness", "unverified_in_memory"),
            "source_identity": meta.get("_source_identity", {"state": UNKNOWN, "independently_verified": False,
                                                              "gaps": ["source binding is unavailable for in-memory inputs"]}),
            "window": {"from": created[0], "to": created[-1]} if created else UNKNOWN, "since_filter": meta.get("since"),
            "sample": {"runs": len(runs), "runs_with_jobs": sum(r["id"] in jobs_by_run for r in runs),
                       "collect_errors": len(meta.get("errors", [])), "incomplete_runs": sum(r.get("status", "completed") != "completed" for r in runs)},
            "data_gaps": meta.get("_load_gaps", []) + [{"code": "conflicting_run_identity", "run_id": r["id"]}
                                                      for r in runs if r.get("_identity_conflict")]
                         + [{"code": "unknown_run_attempt_identity", "run_id": r["id"]}
                            for r in runs if attempt_number(r) is None],
            "collection_completeness": meta.get("_manifest", {}).get("completeness", UNKNOWN),
            "run_enumeration": meta.get("_manifest", {}).get("run_enumeration", UNKNOWN),
            "push_to_green_s": push_to_green(runs, jobs_by_run), "flaky_sha_rate": retry["suspected_retry_recovery_rate"],
            "first_pass_failure_rate": retry["first_pass_failure_rate"], "reliability": retry,
            "required_checks": sorted(required) if required is not None else UNKNOWN, "required_check_evidence": required_state,
            "billable_minutes_by_label": physical["rounded_runner_minutes_estimate_by_label"], "runner_usage": physical,
            "pricing": price, "cost_per_merge": price["cost_per_merge_estimate"], "cache_hit_rate": UNKNOWN,
            "workflows": {key: analyze_group(value, jobs_by_run, required, phase_map) for key, value in sorted(groups.items())},
            "cohorts": [{"identity": json.loads(key), "metrics": analyze_group(value, jobs_by_run, required, phase_map),
                         "missing_attributes": [field for field in COHORT_FIELDS if json.loads(key)[field] == UNKNOWN]}
                        for key, value in sorted(cohorts.items())],
            "graph": evaluate_graph(graph) if graph is not None else {"status": UNKNOWN, "gaps": ["normalized graph absent"]},
            "definitions": DEFINITIONS, "limitations": LIMITATIONS}


def summary_lines(metrics: dict) -> list[str]:
    lines = [f"repo={metrics['repo']} runs={metrics['sample']['runs']} window={metrics['window']}",
             f"diagnostic_push_to_green_s={metrics['push_to_green_s']}", f"freshness={metrics['freshness']}"]
    for name, group in metrics["workflows"].items():
        wall = group["run_wall_s_success"]
        lines.append(f"  {name}: n={group['runs']} wall_p50={wall if wall == UNKNOWN else wall['p50']} "
                     f"wall_p95={wall if wall == UNKNOWN else wall['p95']}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compute diagnostic CI metrics from collection snapshots.")
    parser.add_argument("--input", type=Path, default=Path(".ci-audit"))
    parser.add_argument("--output", type=Path, default=Path(".ci-audit/metrics.json"))
    parser.add_argument("--since", help="only run creation times at/after this timezone-aware ISO-8601 time")
    parser.add_argument("--graph", type=Path, help="normalized ci-job-graph/1 JSON; no YAML inference")
    parser.add_argument("--cohort-context", type=Path, help="ci-cohort-context/1 run annotations")
    parser.add_argument("--phase-map", type=Path, help="reviewed ci-phase-map/1; unmatched bindings remain unknown")
    parser.add_argument("--pricing", type=Path, help="ci-pricing/1 explicit dated runner rates")
    args = parser.parse_args(argv)
    if args.since and ts(args.since) is None:
        parser.error("--since must be ISO-8601 with a timezone")
    try:
        runs, jobs, meta, required = load(args.input)
        if args.since:
            meta = {**meta, "since": args.since}
        extra = {key: json.loads(path.read_text(encoding="utf-8")) if path else None
                 for key, path in (("graph", args.graph), ("cohort_context", args.cohort_context), ("pricing", args.pricing))}
        extra["phase_map"] = reviewed_phases.load(args.phase_map) if args.phase_map else None
        metrics = analyze(runs, jobs, meta, required, **extra)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(metrics, indent=2, allow_nan=False), encoding="utf-8")
    except (OSError, ValueError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("\n".join(summary_lines(metrics)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
