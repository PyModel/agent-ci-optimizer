#!/usr/bin/env python3
"""Publish bounded diagnostic timing metrics, never raw provider payloads or logs."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import re
from pathlib import Path

MAX_BYTES = 16 * 1024 * 1024
MIN_TAIL_RUNS = 40  # Same conservative warning policy as benchmark.py, not a significance test.
NOTICE = ("Protection and security/merge eligibility are NOT ASSESSED. "
          "This manual timing audit is not a required check or a readiness certificate.")
PHASES = ("waiting_s", "setup_s", "running_s", "unattributed_s", "overlap_s", "runner_s")
OUTCOMES = {"success", "failure", "cancelled", "skipped", "neutral", "timed_out", "action_required",
            "stale", "startup_failure", "queued", "in_progress", "waiting", "pending", "unknown"}


def numeric(value: object) -> int | float | str:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 2 ** 53 and math.isfinite(value):
        return value
    return "unknown"


def count(value: object) -> int | str:
    return value if type(value) is int and 0 <= value <= 2 ** 53 else "unknown"


def counts(value: dict) -> dict:
    result = {key: numeric(count) for key, count in value.items() if key in OUTCOMES}
    extras = [numeric(count) for key, count in value.items() if key not in OUTCOMES]
    if extras:
        existing = result.get("unknown", 0)
        result["unknown"] = "unknown" if "unknown" in [existing, *extras] else numeric(existing + sum(extras))
    return result


def distribution(value: object) -> dict | str:
    if not isinstance(value, dict):
        return "unknown"
    n = count(value.get("n"))
    return {**{key: numeric(value.get(key)) for key in ("p50", "p95", "mean", "max")},
            "n": n, "p95_tail_observations": count(value.get("p95_tail_observations")),
            "p95_preliminary": n == "unknown" or n < MIN_TAIL_RUNS,
            "method": "nearest-rank", "low_sample": value.get("low_sample") is True,
            "heavy_tail": value.get("heavy_tail") is True}


def phase(value: dict) -> dict:
    return {**{key: numeric(value.get(key)) for key in
               ("observations", "complete_phase_observations", "unknown_phase_observations")},
            **{key: distribution(value.get(key)) for key in PHASES},
            "outcomes": counts(value.get("outcomes", {})),
            "unknown_by_metric": {key: numeric(value.get("unknown_by_metric", {}).get(key)) for key in PHASES}}


def identifier(value: str) -> tuple[str, str]:
    # Preserve useful labels without collisions from truncation. Never include step names.
    label = "".join(char if char.isprintable() else " " for char in value[:240])
    return hashlib.sha256(value.encode("utf-8")).hexdigest(), label


def project(metrics: dict) -> dict | None:
    """Allowlist only diagnostic timing fields; never forward source dictionaries."""
    scope = metrics.get("collection_scope", {})
    if (metrics.get("schema") != "ci-metrics/2" or scope.get("name") != "timings-only"
            or scope.get("required_checks_requested") is not False
            or scope.get("protection_security_eligibility") != "not_assessed"):
        return None
    workflows = {}
    for workflow, group in metrics.get("workflows", {}).items():
        workflow_id, label = identifier(workflow)
        jobs = {}
        for name, row in group.get("job_phases", {}).items():
            job_id, job_label = identifier(name)
            jobs[job_id] = {"name": job_label, **phase(row),
                           **{key: phase(row.get(key, {})) for key in ("first_attempt", "reruns", "unknown_attempt")}}
        workflows[workflow_id] = {"name": label, "runs": numeric(group.get("runs")),
                                 "conclusions": counts(group.get("conclusions", {})),
                                 "run_wall_s_success": distribution(group.get("run_wall_s_success")), "job_phases": jobs}
    source, sample, enumeration = (metrics.get(key, {}) for key in ("source_identity", "sample", "run_enumeration"))
    repo = metrics.get("repo")
    return {"schema": "ci-timing-audit-metrics/1", "purpose": "diagnostic timing sample, not gate certification",
            "collection_scope": {"name": "timings-only", "protection_security_eligibility": "not_assessed"},
            "repo": repo if isinstance(repo, str) and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) else "unknown",
            "sample": {key: numeric(sample.get(key)) for key in ("runs", "runs_with_jobs", "collect_errors", "incomplete_runs")},
            "run_enumeration": {"requested_limit": numeric(enumeration.get("requested_limit")),
                                "returned_runs": numeric(enumeration.get("returned_runs")),
                                "limit_reached": enumeration.get("limit_reached") if type(enumeration.get("limit_reached")) is bool else "unknown",
                                "retention": "unknown; deleted or expired evidence cannot be inferred"},
            "source_identity": {"state": "collector_verified" if source.get("state") == "collector_verified" else "unknown",
                                "loaded_payload_hashes_verified": source.get("loaded_payload_hashes_verified") is True,
                                "independently_verified": False},
            "collection_completeness": {key: metrics.get("collection_completeness", {}).get(key) is True for key in ("jobs", "attempts")},
            "data_gap_count": len(metrics["data_gaps"]) if isinstance(metrics.get("data_gaps"), list) else "unknown",
            "workflows": workflows}


def unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise ValueError("nonfinite JSON constant")


def cell(value: object) -> str:
    text = html.escape(str(value)[:240]).replace("|", "&#124;").replace("\n", " ").replace("\r", " ")
    return text.translate(str.maketrans({char: "\\" + char for char in "\\`*_[]()!#"}))


def number(value: object) -> str:
    value = numeric(value)
    if value != "unknown":
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return "unknown"


def pair(row: dict, key: str) -> str:
    distribution = row.get(key)
    distribution = distribution if isinstance(distribution, dict) else {}
    n = count(distribution.get("n"))
    missing = count(row.get("unknown_by_metric", {}).get(key))
    values = (f"{number(distribution.get('p50'))} / {number(distribution.get('p95'))}"
              if n != "unknown" and n > 0 else "unknown / unknown")
    preliminary = "; p95 preliminary" if n == "unknown" or n < MIN_TAIL_RUNS else ""
    return f"{values} (n={n}; missing={missing}{preliminary})"


def outcome_summary(row: dict) -> str:
    values = counts(row.get("outcomes", {}))
    return ", ".join(f"{name}: {number(value)}" for name, value in sorted(values.items())) or "unknown"


def render(metrics: dict | None, status: str) -> tuple[str, bool]:
    valid = bool(metrics and metrics.get("schema") == "ci-timing-audit-metrics/1"
                 and metrics.get("collection_scope", {}).get("name") == "timings-only")
    sample = metrics.get("sample", {}) if valid else {}
    complete = bool(valid and status == "success"
                    and metrics.get("source_identity", {}).get("state") == "collector_verified"
                    and metrics.get("source_identity", {}).get("loaded_payload_hashes_verified") is True
                    and all(metrics.get("collection_completeness", {}).get(key) is True for key in ("jobs", "attempts"))
                    and sample.get("collect_errors") == 0 and metrics.get("data_gap_count") == 0
                    and isinstance(sample.get("runs"), int) and 0 <= sample["runs"] <= 150
                    and sample.get("runs_with_jobs") == sample["runs"])
    lines = ["# CI timing audit", "", f"**{NOTICE}**", "",
             f"Timing evidence collection: **{'complete for the requested sample' if complete else 'FAILED OR INCOMPLETE'}**.", "",
             "A green audit means that the requested timing evidence was collected; it does not mean that historical CI passed or met its budgets.", ""]
    if not valid:
        lines += ["No usable timing metrics were produced. Inspect the failed workflow step; no raw response or log is published.", ""]
        return "\n".join(lines), False
    enumeration = metrics.get("run_enumeration", {})
    lines += [f"Repository: {cell(metrics.get('repo', 'unknown'))}. Retained runs sampled: {number(sample.get('runs'))} of at most 150; "
              f"incomplete runs included: {number(sample.get('incomplete_runs'))}.", "",
              f"Sample limit reached: {cell(enumeration.get('limit_reached', 'unknown'))}. "
              "Deleted or expired history is unknown. Jobs from all available attempts, including failures, are retained; attempt history is capped at 100 per run and gaps fail collection.", "",
              "## Job phases (seconds: p50 / p95)", "",
              "These distributions include observed outcomes and attempts. Setup uses the analyzer's documented step-name heuristic. "
              "Missing or censored timings remain unknown; percentile components are not additive. "
              "Each metric shows its own observed count (n) and missing count. P95 is preliminary below "
              f"{MIN_TAIL_RUNS} observations or when n is unknown; reaching that count does not establish significance or budget attainment. "
              "All-attempt rows retain every observed outcome; first-attempt and rerun rows are distinct populations. "
              "Workflow/job labels are bounded; raw step names, branch metadata and unrecognized fields are excluded.", "",
              "| Workflow | Job | Attempt basis | Waiting | Setup | Running | Runner total | Outcomes | Unknown phases |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for workflow, group in metrics.get("workflows", {}).items():
        for name, row in group.get("job_phases", {}).items():
            populations = [("All observed attempts", row)]
            for key, label in (("first_attempt", "First attempts"), ("reruns", "Reruns"), ("unknown_attempt", "Unknown attempt")):
                population = row.get(key, {})
                observations = count(population.get("observations"))
                if observations != "unknown" and observations > 0:
                    populations.append((label, population))
            for basis, population in populations:
                lines.append("| " + " | ".join([cell(group.get("name", workflow)), cell(row.get("name", name)), basis,
                             *(pair(population, key) for key in ("waiting_s", "setup_s", "running_s", "runner_s")),
                             outcome_summary(population), number(population.get("unknown_phase_observations"))]) + " |")
    lines += ["", "Historical CI cost is unknown without dated provider rates and billing evidence. "
              "Cache hit rates and enforced merge-queue/full-suite policy are not established by this report.", ""]
    return "\n".join(lines), complete


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--collection-status", choices=("success", "failure", "cancelled", "skipped"), required=True)
    args = parser.parse_args(argv)
    metrics = None
    try:
        with args.metrics.open("rb") as stream:
            payload = stream.read(MAX_BYTES + 1)
        if len(payload) > MAX_BYTES:
            raise ValueError("metrics too large")
        candidate = json.loads(payload, object_pairs_hook=unique_object, parse_constant=reject_constant)
        if isinstance(candidate, dict):
            metrics = project(candidate)
        report, complete = render(metrics, args.collection_status)
    except (OSError, ValueError, TypeError, AttributeError, RecursionError, OverflowError):
        metrics = None
        report, complete = render(None, args.collection_status)
    try:
        # A new directory prevents a failed audit from publishing old metrics.
        args.out.mkdir(parents=True, exist_ok=False)
        (args.out / "report.md").write_text(report, encoding="utf-8")
        (args.out / "status.json").write_text(json.dumps({"schema": "ci-timing-audit/1", "notice": NOTICE,
            "collection_status": args.collection_status, "timing_sample_complete": complete}, indent=2) + "\n", encoding="utf-8")
        if metrics is not None:
            (args.out / "metrics.json").write_text(json.dumps(metrics, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    except (OSError, ValueError, RecursionError):
        return 2
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
