#!/usr/bin/env python3
"""Verify one GitHub Actions attempt's job budgets from offline JSON evidence.

--jobs accepts a jobs API response or a --paginate --slurp array of response
pages. --policy is {"schema":"ci-job-budgets/1","jobs":{"exact job name":{
"waiting_s":10,"setup_s":30,"running_s":60,"runner_s":120,
"setup_end_step":"Set up Python","work_steps":["Test"]}}}.
Every limit is strict (<). The setup marker is the LAST setup step, not work.
Waiting = job started-created; setup = first work start-job started; running =
union of the explicitly named work intervals; runner = job completed-started.
Job-created-to-started is the API waiting interval, not proven ready-to-run time.

Required --run-id and --head-sha bind evidence to the caller's expected run.
Inputs are capped at 16 MiB each, 100 pages and 1,000 jobs. Only attempt 1 is
accepted. All expected jobs and all pages are required. One
explicit --current-job may still run: its setup marker must have succeeded and
its first work step must have started. Its running/runner budgets remain pending
and MUST be bounded externally by run_budget.py/job timeout. Optional
--current-job-setup accepts a ci-current-job-setup/1 record written by the trusted
workflow during its first work step, after successful setup. It binds job, run,
head and attempt, and uses observed_at as a conservative setup upper bound when
the live API timeline lags. It never replaces an observed failure or changes
completed-job validation. This caller-supplied record is not an attestation.
Without it, setup still requires complete API evidence. No API calls, writes,
retries, percentile certification, or proof of branch/merge-queue rules occurs.
Exit 0: observed budgets pass (possibly with explicit pending current-job limits);
1: failure/incomplete evidence; 2: malformed input or policy.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sys

METRICS = ("waiting_s", "setup_s", "running_s", "runner_s")
POLICY_FIELDS = set(METRICS) | {"setup_end_step", "work_steps"}
MAX_INPUT_BYTES, MAX_PAGES, MAX_JOBS = 16 * 1024 * 1024, 100, 1000
SHA = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\Z")
CURRENT_SETUP_FIELDS = {"schema", "job", "run_id", "head_sha", "run_attempt",
                        "setup_end_step", "first_work_step", "observed_at"}


def text(value):
    return isinstance(value, str) and bool(value.strip()) and "\0" not in value


def positive_int(value):
    return type(value) is int and value > 0


def valid_limit(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        return False


def timestamp(value, field):
    if not isinstance(value, str):
        raise ValueError(f"missing or invalid {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        seconds = parsed.timestamp()
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError(f"missing or invalid {field}") from exc
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"missing or invalid {field}")
    return seconds


def validate_policy(policy):
    if not isinstance(policy, dict) or set(policy) != {"schema", "jobs"} or policy["schema"] != "ci-job-budgets/1":
        raise ValueError("policy requires schema ci-job-budgets/1 and jobs")
    expected = policy["jobs"]
    if not isinstance(expected, dict) or not expected or len(expected) > MAX_JOBS:
        raise ValueError("policy jobs must be a non-empty object of exact names")
    for name, limits in expected.items():
        if not text(name) or not isinstance(limits, dict) or set(limits) != POLICY_FIELDS:
            raise ValueError("each policy job needs exactly four limits, setup_end_step and work_steps")
        for metric in METRICS:
            value = limits[metric]
            if not valid_limit(value):
                raise ValueError("budget limits must be positive finite numbers")
        work = limits["work_steps"]
        if not text(limits["setup_end_step"]) or not isinstance(work, list) or not work or any(not text(s) for s in work):
            raise ValueError("setup_end_step and non-empty work_steps must name exact steps")
        if len(set(work)) != len(work) or limits["setup_end_step"] in work:
            raise ValueError("work_steps must be unique and exclude the setup marker")
    return expected


def unpack_jobs(payload):
    pages = payload if isinstance(payload, list) else [payload]
    if not pages or len(pages) > MAX_PAGES:
        raise ValueError("jobs evidence requires 1 to 100 API response pages")
    jobs, totals = [], []
    for page in pages:
        if not isinstance(page, dict) or type(page.get("total_count")) is not int or page["total_count"] < 0 or not isinstance(page.get("jobs"), list):
            raise ValueError("each jobs page requires nonnegative integer total_count and jobs array")
        if any(not isinstance(job, dict) for job in page["jobs"]):
            raise ValueError("each job must be an object")
        if page["total_count"] > MAX_JOBS or len(jobs) + len(page["jobs"]) > MAX_JOBS:
            raise ValueError("jobs evidence exceeds the 1000-job limit")
        totals.append(page["total_count"])
        jobs.extend(page["jobs"])
    errors = []
    if len(set(totals)) != 1 or totals[0] != len(jobs):
        errors.append("job pagination is incomplete or inconsistent with total_count")
    return jobs, errors


def union_seconds(intervals):
    total, end = 0.0, None
    for start, stop in sorted(intervals):
        total += stop - max(start, end) if end is not None and stop > end else (stop - start if end is None else 0)
        end = max(end, stop) if end is not None else stop
    return total


def validate_current_setup(record, current_job, run_id, head_sha, attempt, now):
    if current_job is None or run_id is None or head_sha is None:
        raise ValueError("local setup evidence requires current-job and bound run/head identity")
    if not isinstance(record, dict) or set(record) != CURRENT_SETUP_FIELDS or record["schema"] != "ci-current-job-setup/1":
        raise ValueError("local setup evidence requires exactly the ci-current-job-setup/1 fields")
    if (record["job"] != current_job or not positive_int(record["run_id"]) or record["run_id"] != run_id
            or type(record["run_attempt"]) is not int or record["run_attempt"] != attempt
            or not isinstance(record["head_sha"], str) or SHA.fullmatch(record["head_sha"]) is None
            or record["head_sha"].lower() != head_sha.lower()):
        raise ValueError("local setup evidence does not match the current job/run/head/attempt")
    if not text(record["setup_end_step"]) or not text(record["first_work_step"]):
        raise ValueError("local setup evidence must name its setup and first work boundaries")
    observed = timestamp(record["observed_at"], "local setup observed_at")
    clock = datetime.now(timezone.utc).timestamp() if now is None else timestamp(now, "verification clock")
    if observed > clock:
        raise ValueError("local setup timestamp is later than the verification clock")
    return {**record, "observed_s": observed}


def check_job(job, limits, current, current_setup=None):
    result = {"name": job["name"], "measurements": {key: "unknown" for key in METRICS},
              "limits": {key: limits[key] for key in METRICS}, "pending": [], "errors": []}
    try:
        if job.get("run_attempt") != 1 or type(job.get("run_attempt")) is not int:
            raise ValueError("missing attempt evidence or rerun detected")
        if not {"conclusion", "completed_at"} <= job.keys():
            raise ValueError("job completion evidence is missing")
        live = current and job.get("status") == "in_progress"
        if current_setup is not None and not live:
            raise ValueError("local setup evidence applies only to the live current job")
        if not live and (job.get("status") != "completed" or job.get("conclusion") != "success"):
            raise ValueError("job must complete successfully; skipped, failed, cancelled or incomplete jobs cannot pass")
        if live and (job.get("conclusion") is not None or job.get("completed_at") is not None):
            raise ValueError("current job has contradictory completion evidence")
        created = timestamp(job.get("created_at"), "job created_at")
        started = timestamp(job.get("started_at"), "job started_at")
        finished = None if live else timestamp(job.get("completed_at"), "job completed_at")
        if started < created or (finished is not None and finished < started):
            raise ValueError("job timestamps are out of order")
        if current_setup is not None and current_setup["observed_s"] < started:
            raise ValueError("local setup timestamp precedes the API job start")
        steps = job.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("step evidence is missing")
        by_name, previous_number, previous_start, unfinished = {}, 0, started, False
        for step in steps:
            if not isinstance(step, dict) or not text(step.get("name")) or step["name"] in by_name:
                raise ValueError("missing or duplicate step name")
            number = step.get("number")
            if not positive_int(number) or number <= previous_number:
                raise ValueError("step numbers must be unique and ordered")
            previous_number = number
            if not {"status", "conclusion", "started_at", "completed_at"} <= step.keys():
                raise ValueError("step state or timing evidence is missing")
            state, conclusion = step.get("status"), step.get("conclusion")
            if state in ("queued", "pending") and live:
                if any(step.get(key) is not None for key in ("started_at", "completed_at", "conclusion")):
                    raise ValueError("queued step has contradictory timing or conclusion")
                by_name[step["name"]] = (None, None, state)
                unfinished = True
                continue
            if unfinished:
                raise ValueError("a step executed after an unfinished step")
            if state not in ("completed", "in_progress") or (state == "in_progress" and not live):
                raise ValueError("step has missing or unexpected execution state")
            if (state == "completed" and conclusion != "success") or (state == "in_progress" and conclusion is not None):
                raise ValueError("non-success or contradictory step conclusion")
            begin = timestamp(step.get("started_at"), "step started_at")
            end = timestamp(step.get("completed_at"), "step completed_at") if state == "completed" else None
            if state == "in_progress" and step.get("completed_at") is not None:
                raise ValueError("running step has a completion timestamp")
            unfinished = state == "in_progress"
            if begin < previous_start or (end is not None and end < begin) or (finished is not None and (begin > finished or end > finished)):
                raise ValueError("step timestamps are out of order or outside the job")
            previous_start = begin
            by_name[step["name"]] = (begin, end, state)
        marker = by_name.get(limits["setup_end_step"])
        if marker is None:
            raise ValueError("setup_end_step must exist")
        if current_setup is None and marker[2] != "completed":
            raise ValueError("setup_end_step must exist and have completed successfully")
        work = [by_name.get(name) for name in limits["work_steps"]]
        if any(step is None for step in work):
            raise ValueError("an explicit work step is missing")
        # The first work step is the earliest declared work step in execution
        # order, not whichever later work step happens to have a timestamp.
        first_name, first_step = next((name, step) for name, step in by_name.items() if name in limits["work_steps"])
        first_work = first_step[0]
        if first_work is None and current_setup is None:
            raise ValueError("first work start is unknown")
        if marker[1] is not None and first_work is not None and marker[1] > first_work:
            raise ValueError("setup marker must complete before work starts")
        if current_setup is not None:
            if (current_setup["setup_end_step"] != limits["setup_end_step"]
                    or current_setup["first_work_step"] != first_name):
                raise ValueError("local setup boundaries do not match the policy and step order")
            boundary = current_setup["observed_s"]
            # The record is written within the first work step, so its start and
            # all preceding setup timestamps must be no later than the record.
            # Keep the API states untouched: local evidence supplies a bound,
            # never an invented success, duration or completed work interval.
            for name, step in by_name.items():
                observed_times = (step[0],) if name == first_name else step[:2]
                if any(value is not None and value > boundary for value in observed_times):
                    raise ValueError("API setup or first work timestamp exceeds the local boundary")
                if name == first_name:
                    break
            setup = boundary - started
            result["setup_evidence"] = {"source": "local_workflow_boundary", "kind": "conservative_upper_bound"}
        else:
            setup = first_work - started
        measurements = result["measurements"]
        measurements.update(waiting_s=started - created, setup_s=setup)
        if live:
            result["pending"] = ["running_s", "runner_s"]
            measurements.update(running_s="pending", runner_s="pending")
        else:
            if any(step[2] != "completed" for step in work):
                raise ValueError("every explicit work step must complete successfully")
            measurements.update(running_s=union_seconds([(step[0], step[1]) for step in work]), runner_s=finished - started)
        for metric, value in measurements.items():
            if isinstance(value, (int, float)) and value >= limits[metric]:
                result["errors"].append(f"{metric} meets or exceeds its strict limit")
    except ValueError as exc:
        result["errors"].append(str(exc))
    if result["errors"] or (current and job.get("status") == "in_progress"):
        # Step names can contain shell commands. Emit only bounded state labels
        # and timestamp-presence flags, never names, commands, env or raw values.
        steps = job.get("steps")
        if isinstance(steps, list):
            result["step_states"] = [{
                "number": step.get("number") if positive_int(step.get("number")) else "invalid",
                "status": step.get("status") if step.get("status") in ("queued", "pending", "in_progress", "completed") else "invalid",
                "conclusion": step.get("conclusion") if step.get("conclusion") in (
                    None, "success", "failure", "skipped", "cancelled", "timed_out", "neutral", "action_required", "stale") else "invalid",
                "has_started_at": step.get("started_at") is not None,
                "has_completed_at": step.get("completed_at") is not None,
            } if isinstance(step, dict) else {"record": "invalid"} for step in steps[:100]]
            result["step_states_omitted"] = max(0, len(steps) - 100)
    return result


def verify(payload, policy, attempt=1, current_job=None, *, expected_run_id=None, expected_head_sha=None,
           current_job_setup=None, now=None):
    expected = validate_policy(policy)
    bound = expected_run_id is not None or expected_head_sha is not None
    if bound and (not positive_int(expected_run_id) or not isinstance(expected_head_sha, str) or SHA.fullmatch(expected_head_sha) is None):
        raise ValueError("expected identity needs a positive run_id and a full hexadecimal head_sha")
    if type(attempt) is not int or attempt != 1:
        raise ValueError("only attempt 1 is accepted; reruns cannot certify this policy")
    if current_job is not None and current_job not in expected:
        raise ValueError("current-job must exactly match a policy job")
    local_setup = None if current_job_setup is None else validate_current_setup(
        current_job_setup, current_job, expected_run_id, expected_head_sha, attempt, now)
    jobs, errors = unpack_jobs(payload)
    ids, names, runs, heads, results = set(), set(), set(), set(), []
    for job in jobs:
        identity, name = job.get("id"), job.get("name")
        if not positive_int(identity) or identity in ids or not text(name) or name in names:
            errors.append("missing/duplicate job ID or name")
            continue
        ids.add(identity)
        names.add(name)
        run, head = job.get("run_id"), job.get("head_sha")
        if not positive_int(run) or not isinstance(head, str) or SHA.fullmatch(head) is None:
            errors.append("run_id/head_sha provenance is missing or invalid")
        else:
            runs.add(run)
            heads.add(head.lower())
        if name not in expected:
            errors.append("an unexpected job is absent from the exact policy")
            continue
        results.append(check_job(job, expected[name], name == current_job,
                                 local_setup if name == current_job else None))
    if len(runs) != 1 or len(heads) != 1:
        errors.append("job run_id/head_sha provenance is inconsistent or unavailable")
    if bound and (runs != {expected_run_id} or heads != {expected_head_sha.lower()}):
        errors.append("job evidence does not match the caller's expected run_id/head_sha")
    if set(expected) - names:
        errors.append("one or more expected jobs are missing")
    failed = bool(errors) or any(job["errors"] for job in results)
    pending = any(job["pending"] for job in results)
    return {"schema": "ci-job-budget-result/1", "attempt": attempt, "identity_bound": bound,
            "scope": "one workflow attempt; no percentile or branch-rule certification",
            "status": "fail" if failed else ("pass_with_pending" if pending else "pass"),
            "run_id": next(iter(runs)) if len(runs) == 1 else "unknown",
            "head_sha": next(iter(heads)) if len(heads) == 1 else "unknown",
            "errors": errors, "jobs": results}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def read_json(path):
    with path.open("rb") as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("JSON input exceeds the 16 MiB limit")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jobs", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--attempt", type=int, required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--current-job")
    parser.add_argument("--current-job-setup", type=Path,
                        help="trusted local first-work boundary; current job only, never completed jobs")
    args = parser.parse_args(argv)
    try:
        payload, policy = read_json(args.jobs), read_json(args.policy)
        result = verify(payload, policy, args.attempt, args.current_job,
                        expected_run_id=args.run_id, expected_head_sha=args.head_sha,
                        current_job_setup=read_json(args.current_job_setup) if args.current_job_setup else None)
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        error = str(exc) if type(exc) is ValueError else "could not read valid JSON input"
        print(json.dumps({"schema": "ci-job-budget-result/1", "status": "invalid_input", "errors": [error]}))
        return 2
    print(json.dumps(result, indent=2, allow_nan=False))
    return 1 if result["status"] == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
