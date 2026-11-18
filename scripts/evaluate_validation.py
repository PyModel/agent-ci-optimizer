#!/usr/bin/env python3
"""Evaluate a reviewed four-category policy against normalized, read-only evidence.

This is a policy evaluator, not an evidence collector or signature verifier. An
adapter must verify provider identities, attestations and telemetry before making
the assertions in the input. Missing or contradictory evidence never passes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REVISION = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})\Z")
DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")
CATEGORIES = ("rules", "change", "integration", "release")
STATES = {"completed", "running", "failed", "cancelled", "superseded"}


def stamp(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo is not None else None
    except ValueError:
        return None


def number(value, *, minimum=0, integer=False):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= minimum
            and (not integer or isinstance(value, int)))


def text(value):
    return isinstance(value, str) and bool(value.strip())


def digest(policy):
    raw = json.dumps(policy, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def validate_policy(policy):
    if not isinstance(policy, dict) or policy.get("schema") != "ci-validation-policy/1":
        raise ValueError("policy schema must be ci-validation-policy/1")
    if not text(policy.get("repository")):
        raise ValueError("policy repository is required")
    for key in ("feedback_budget_s", "minimum_pr_samples", "max_evidence_age_s"):
        if not number(policy.get(key), minimum=1, integer=key == "minimum_pr_samples"):
            raise ValueError(f"{key} must be positive")
    categories = policy.get("categories")
    if not isinstance(categories, dict) or set(categories) != set(CATEGORIES):
        raise ValueError("all four category definitions are required")
    identities = {}
    for category, requirements in categories.items():
        if not isinstance(requirements, list) or not requirements:
            raise ValueError(f"{category} requires at least one explicit check")
        category_identities = set()
        for requirement in requirements:
            if not isinstance(requirement, dict) or not text(requirement.get("identity")):
                raise ValueError("every check needs a qualified identity")
            identity = requirement["identity"]
            allowed = requirement.get("allowed_skip_reasons", [])
            if not isinstance(allowed, list) or not all(text(x) for x in allowed):
                raise ValueError("allowed_skip_reasons must be an explicit string list")
            if identity in category_identities:
                raise ValueError("check identities must be unique within a category")
            if identity in identities and identities[identity] != sorted(set(allowed)):
                raise ValueError("a shared check must have the same skip contract in every category")
            category_identities.add(identity)
            identities[identity] = sorted(set(allowed))
    integration = policy.get("integration", {})
    if not isinstance(integration, dict) or integration.get("mode") not in {"merge_queue", "rolling", "all_pr"}:
        raise ValueError("integration mode must be merge_queue, rolling or all_pr")
    for key in ("max_green_age_s", "max_running_age_s", "max_unverified_commits"):
        if not number(integration.get(key), minimum=0 if key == "max_unverified_commits" else 1,
                      integer=key == "max_unverified_commits"):
            raise ValueError(f"integration.{key} is required and must be bounded")
    rollout = policy.get("rollout")
    if not isinstance(rollout, dict) or type(rollout.get("enabled")) is not bool:
        raise ValueError("rollout.enabled must be explicitly true or false")
    if rollout["enabled"]:
        stages = rollout.get("stages_pct")
        if (not isinstance(stages, list) or not stages or
                not all(number(x, minimum=0.000001) and x <= 100 for x in stages) or
                stages != sorted(set(stages)) or stages[-1] != 100):
            raise ValueError("rollout stages must increase to 100 percent")
        for key in ("minimum_samples", "max_age_s", "minimum_observation_s"):
            if not number(rollout.get(key), minimum=1, integer=key == "minimum_samples"):
                raise ValueError(f"rollout.{key} must be positive")
        metrics = rollout.get("metrics")
        if not isinstance(metrics, dict) or not metrics:
            raise ValueError("rollout requires explicit metric limits")
        for name, limit in metrics.items():
            if not text(name) or not isinstance(limit, dict) or not limit:
                raise ValueError("invalid metric limit")
            if set(limit) - {"max", "max_relative_increase"}:
                raise ValueError("unsupported metric limit")
            if not all(number(x) for x in limit.values()):
                raise ValueError("metric limits must be finite and non-negative")


def result(state, reasons=(), **values):
    return {"state": state, "reasons": list(reasons), **values}


def check_gate(record, requirements, policy_digest, now, *, artifact_digest=None):
    """Latest attempt is scoped to one check identity on one exact revision."""
    if not isinstance(record, dict):
        return result("unknown", ["record missing"])
    revision = record.get("revision")
    received = stamp(record.get("received_at"))
    if not isinstance(revision, str) or not REVISION.fullmatch(revision):
        return result("unknown", ["immutable revision missing"])
    if received is None or received > now:
        return result("unknown", ["record receipt time missing or in the future"])
    if record.get("policy_digest") != policy_digest:
        return result("unknown", ["policy identity mismatch"])
    if record.get("checks_complete") is not True or not isinstance(record.get("checks"), list):
        return result("unknown", ["check enumeration incomplete"])
    checks = record["checks"]
    if any(not isinstance(c, dict) for c in checks):
        return result("unknown", ["malformed check"])
    ends, reasons, unknown = [], [], []
    for requirement in requirements:
        identity = requirement["identity"]
        matches = [c for c in checks if c.get("identity") == identity]
        if not matches:
            unknown.append(f"missing check: {identity}")
            continue
        if any(not number(c.get("attempt"), minimum=1, integer=True) for c in matches):
            unknown.append(f"invalid attempt identity: {identity}")
            continue
        latest = max(c["attempt"] for c in matches)
        candidates = [c for c in matches if c["attempt"] == latest]
        if len(candidates) != 1:
            unknown.append(f"duplicate latest check: {identity}")
            continue
        check = candidates[0]
        if (check.get("revision") != revision or check.get("policy_digest") != policy_digest
                or not text(check.get("source")) or ("input_digest" in record and
                check.get("input_digest") != record["input_digest"])):
            unknown.append(f"check provenance mismatch: {identity}")
            continue
        if artifact_digest is not None and check.get("artifact_digest") != artifact_digest:
            reasons.append(f"check artifact provenance mismatch: {identity}")
            continue
        conclusion = check.get("conclusion")
        if conclusion in {"failure", "cancelled", "timed_out", "action_required", "stale"}:
            reasons.append(f"{identity}: {conclusion}")
            continue
        end = stamp(check.get("completed_at"))
        if end is None or not received <= end <= now:
            unknown.append(f"invalid completion time: {identity}")
            continue
        earlier = [stamp(c.get("completed_at")) for c in matches if c["attempt"] < latest]
        if any(t is not None and t > end for t in earlier):
            unknown.append(f"attempt chronology conflicts: {identity}")
            continue
        if conclusion == "skipped":
            if (check.get("skip_reason") not in requirement.get("allowed_skip_reasons", [])
                    or check.get("selection_verified") is not True):
                reasons.append(f"unauthorized skip: {identity}")
                continue
        elif conclusion != "success":
            unknown.append(f"{identity}: no explicit success")
            continue
        ends.append(end)
    if reasons:
        return result("blocked", reasons + unknown)
    if unknown:
        return result("unknown", unknown)
    end = max(ends)
    return result("pass", completed_at=end.isoformat(), duration_s=(end - received).total_seconds())


def latest_records(records, key):
    """Tied event times are ambiguous; array order cannot select a green result."""
    grouped, issues = {}, []
    for record in records:
        if not isinstance(record, dict) or not text(record.get(key)) or stamp(record.get("received_at")) is None:
            issues.append("malformed event identity or receipt time")
            continue
        grouped.setdefault(record[key], []).append(record)
    selected = {}
    for identity, group in grouped.items():
        newest = max(stamp(r["received_at"]) for r in group)
        candidates = [r for r in group if stamp(r["received_at"]) == newest]
        if len(candidates) != 1:
            issues.append(f"ambiguous latest event for {identity}")
        else:
            selected[identity] = candidates[0]
    return selected, grouped, issues


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] if ordered else None


def pr_budget(policy, evidence, policy_digest, now):
    events = evidence.get("pr_events")
    if not isinstance(events, list):
        return result("unknown", ["PR event history missing"])
    selected, grouped, issues = latest_records(events, "pr")
    if evidence.get("pr_events_complete") is not True:
        issues.append("PR event history incomplete")
    requirements = policy["categories"]["rules"] + policy["categories"]["change"]
    rows, durations, journeys, event_breaches, inflight_breaches = [], [], [], [], []
    counts = {state: 0 for state in STATES}
    for record in events:
        if isinstance(record, dict) and record.get("state") in counts:
            counts[record["state"]] += 1
            event_gate = check_gate(record, requirements, policy_digest, now)
            if (record["state"] == "completed" and event_gate["state"] == "pass"
                    and event_gate["duration_s"] > policy["feedback_budget_s"]):
                event_breaches.append({"pr": record.get("pr"), "revision": record.get("revision"),
                                       "received_at": record.get("received_at"), "duration_s": event_gate["duration_s"],
                                       "outcome": "green"})
            elif record["state"] in {"failed", "cancelled"}:
                received, finished = stamp(record.get("received_at")), stamp(record.get("completed_at"))
                if (received is not None and finished is not None and received <= finished <= now
                        and record.get("policy_digest") == policy_digest and text(record.get("source"))
                        and isinstance(record.get("revision"), str) and REVISION.fullmatch(record["revision"])):
                    elapsed = (finished - received).total_seconds()
                    if elapsed > policy["feedback_budget_s"]:
                        event_breaches.append({"pr": record.get("pr"), "revision": record["revision"],
                                               "received_at": record["received_at"], "duration_s": elapsed,
                                               "outcome": record["state"]})
        else:
            issues.append("unknown PR event state")
    for pr, record in sorted(selected.items()):
        gate = check_gate(record, requirements, policy_digest, now)
        if record.get("state") != "completed":
            gate = result("blocked" if record.get("state") in {"failed", "cancelled", "superseded"} else "unknown",
                          [f"latest PR event is {record.get('state', 'unknown')}"])
        row = {"pr": pr, "revision": record.get("revision"), "gate": gate,
               "latest_revision_feedback_s": None, "first_push_to_current_green_s": None,
               "over_budget": None, "unfinished_elapsed_s": None}
        if record.get("state") == "running" and record.get("policy_digest") == policy_digest:
            elapsed = (now - stamp(record["received_at"])).total_seconds()
            if elapsed >= 0:
                row["unfinished_elapsed_s"] = elapsed
                if elapsed > policy["feedback_budget_s"]:
                    inflight_breaches.append({"pr": pr, "revision": record.get("revision"), "elapsed_s": elapsed})
        if gate["state"] == "pass":
            duration = gate["duration_s"]
            first = min(stamp(r["received_at"]) for r in grouped[pr])
            journey = (stamp(gate["completed_at"]) - first).total_seconds()
            row.update(latest_revision_feedback_s=duration, first_push_to_current_green_s=journey,
                       over_budget=duration > policy["feedback_budget_s"])
            durations.append(duration)
            journeys.append(journey)
        rows.append(row)
    breaches = [r["pr"] for r in rows if r["over_budget"]]
    insufficient = bool(issues) or len(durations) < policy["minimum_pr_samples"] or any(
        r["gate"]["state"] != "pass" for r in rows)
    any_breach = bool(event_breaches or inflight_breaches)
    state = "breach" if any_breach else "insufficient" if insufficient else "pass"
    return result(state, sorted(set(issues)), target_s=policy["feedback_budget_s"],
                  n_successful_latest=len(durations), n_latest=len(rows), event_counts=counts,
                  latest_revision_p95_s=percentile(durations, .95),
                  first_push_p95_s=percentile(journeys, .95), breaches=breaches,
                  completed_event_breaches=event_breaches, inflight_breaches=inflight_breaches, prs=rows,
                  action="investigate_and_optimize" if any_breach else "collect_evidence" if insufficient else "monitor",
                  definition="Latest exact revision receipt to all rules/change checks; approval wait included if present in receipt interval. First-push journey reported separately.")


def integrations(policy, evidence, policy_digest, now):
    records = evidence.get("integration_records")
    if not isinstance(records, list) or evidence.get("integration_records_complete") is not True:
        return result("unknown", ["integration history incomplete"]), {}
    selected, _, issues = latest_records(records, "revision")
    evaluated = {}
    for revision, record in selected.items():
        gate = check_gate(record, policy["categories"]["integration"], policy_digest, now)
        if record.get("state") != "completed":
            gate = result("blocked" if record.get("state") in {"failed", "cancelled", "superseded"} else "unknown",
                          [f"integration record is {record.get('state', 'unknown')}"])
        if not DIGEST.fullmatch(str(record.get("input_digest", ""))):
            gate = result("unknown", ["integration build-input digest missing"])
        evaluated[revision] = {"record": record, "gate": gate}
    state = evidence.get("integration_state", {})
    if not isinstance(state, dict):
        state = {}
    head = state.get("main_head_revision")
    lag = state.get("unverified_commits")
    if not isinstance(head, str) or not REVISION.fullmatch(head):
        issues.append("main head identity missing")
    if not number(lag, integer=True):
        issues.append("unverified commit count missing")
    good = [x for x in evaluated.values() if x["gate"]["state"] == "pass"]
    newest = max(good, key=lambda x: stamp(x["gate"]["completed_at"])) if good else None
    age = (now - stamp(newest["gate"]["completed_at"])).total_seconds() if newest else None
    if newest is None:
        issues.append("no verified full-suite completion")
    if lag == 0 and (head not in evaluated or evaluated[head]["gate"]["state"] != "pass"):
        issues.append("zero lag lacks a passing full-suite record for main head")
    breaches = []
    if number(lag) and lag > policy["integration"]["max_unverified_commits"]:
        breaches.append("unverified commit bound exceeded")
    if age is not None and age > policy["integration"]["max_green_age_s"]:
        breaches.append("full-suite freshness bound exceeded")
    active_age = None
    if type(state.get("active")) is not bool:
        issues.append("active full-suite state missing")
    elif state["active"]:
        active = stamp(state.get("active_started_at"))
        if active is None or active > now:
            issues.append("active full-suite start missing or invalid")
        else:
            active_age = (now - active).total_seconds()
            if active_age > policy["integration"]["max_running_age_s"]:
                breaches.append("full-suite starvation/running-age bound exceeded")
    return result("blocked" if breaches else "unknown" if issues else "pass", breaches + issues,
                  mode=policy["integration"]["mode"], last_green_age_s=age,
                  unverified_commits=lag, active_age_s=active_age,
                  action="apply_backpressure_and_fix_forward" if breaches else "monitor" if not issues else "collect_evidence"), evaluated


def release_gate(policy, evidence, policy_digest, now, integration_health, integrated):
    candidate = evidence.get("release_candidate")
    if not isinstance(candidate, dict):
        return result("unknown", ["release candidate missing"])
    if candidate.get("state") != "completed":
        return result("blocked" if candidate.get("state") in {"failed", "cancelled", "superseded"} else "unknown",
                      [f"release record is {candidate.get('state', 'unknown')}"])
    artifact = candidate.get("artifact_digest")
    if not isinstance(artifact, str) or not DIGEST.fullmatch(artifact):
        return result("blocked", ["immutable artifact digest missing"])
    gate = check_gate(candidate, policy["categories"]["release"], policy_digest, now, artifact_digest=artifact)
    if gate["state"] != "pass":
        return gate
    revision, artifact = candidate.get("revision"), candidate.get("artifact_digest")
    inputs = candidate.get("input_digest")
    full = integrated.get(revision)
    reasons = []
    if integration_health["state"] != "pass":
        reasons.append("integration freshness/backpressure policy is not satisfied")
    if not full or full["gate"]["state"] != "pass":
        reasons.append("exact source revision lacks a current passing full-suite record")
    elif full["record"].get("input_digest") != inputs:
        reasons.append("integration and release inputs differ")
    elif (now - stamp(full["gate"]["completed_at"])).total_seconds() > policy["integration"]["max_green_age_s"]:
        reasons.append("exact candidate full-suite evidence is stale")
    for label, value in (("artifact", artifact), ("input", inputs)):
        if not isinstance(value, str) or not DIGEST.fullmatch(value):
            reasons.append(f"immutable {label} digest missing")
    provenance = candidate.get("provenance", {})
    verified_at = stamp(provenance.get("verified_at")) if isinstance(provenance, dict) else None
    if (not isinstance(provenance, dict) or provenance.get("verified") is not True
            or not text(provenance.get("source")) or verified_at is None or verified_at > now
            or provenance.get("artifact_digest") != artifact or provenance.get("revision") != revision
            or provenance.get("input_digest") != inputs or provenance.get("policy_digest") != policy_digest):
        reasons.append("verified artifact provenance does not match candidate")
    return result("blocked" if reasons else "pass", reasons, revision=revision, artifact_digest=artifact)


def rollout_gate(policy, evidence, now, release):
    rules = policy["rollout"]
    if not rules["enabled"]:
        return result("not_applicable", ["product policy does not use traffic rollout"])
    if release["state"] != "pass":
        return result("pause", ["release eligibility is not established"])
    deployment = evidence.get("rollout_state")
    if not isinstance(deployment, dict):
        return result("pause", ["verified deployment and stage identity missing"])
    stage_started = stamp(deployment.get("stage_started_at"))
    if (not text(deployment.get("deployment_id")) or not text(deployment.get("environment"))
            or deployment.get("artifact_digest") != release["artifact_digest"]
            or deployment.get("revision") != release["revision"]
            or deployment.get("stage_pct") not in rules["stages_pct"] or type(deployment.get("stage_pct")) is bool
            or stage_started is None or stage_started > now):
        return result("pause", ["deployment or active stage identity is invalid"])
    telemetry = evidence.get("rollout_observation")
    if not isinstance(telemetry, dict):
        return result("pause", ["telemetry missing; initial deployment requires separate authorization"])
    if (any(telemetry.get(key) != deployment[key] for key in
            ("deployment_id", "environment", "artifact_digest", "revision", "stage_pct"))
            or stamp(telemetry.get("stage_started_at")) != stage_started
            or not text(telemetry.get("source"))):
        return result("pause", ["telemetry provenance mismatch"])
    stage = telemetry.get("stage_pct")
    if stage not in rules["stages_pct"] or type(stage) is bool:
        return result("pause", ["unknown rollout stage"])
    started, ended = stamp(telemetry.get("window_started_at")), stamp(telemetry.get("window_ended_at"))
    if (started is None or ended is None or not stage_started <= started < ended <= now
            or (now - ended).total_seconds() > rules["max_age_s"]
            or (ended - started).total_seconds() < rules["minimum_observation_s"]):
        return result("pause", ["telemetry window is stale, invalid or too short"])
    if telemetry.get("complete") is not True or not number(telemetry.get("samples"), integer=True,
                                                            minimum=rules["minimum_samples"]):
        return result("pause", ["telemetry incomplete or sample size insufficient"])
    measured = telemetry.get("metrics", {})
    baseline = telemetry.get("baseline", {})
    if not isinstance(measured, dict) or not isinstance(baseline, dict):
        return result("pause", ["metric evidence malformed"])
    missing, exceeded = [], []
    comparable_baseline = True
    if any("max_relative_increase" in limits for limits in rules["metrics"].values()):
        baseline_start = stamp(telemetry.get("baseline_window_started_at"))
        baseline_end = stamp(telemetry.get("baseline_window_ended_at"))
        if (telemetry.get("baseline_comparable") is not True or not text(telemetry.get("baseline_source"))
                or not text(telemetry.get("baseline_context")) or baseline_start is None or baseline_end is None
                or not baseline_start < baseline_end <= now):
            comparable_baseline = False
            missing.append("comparable baseline source, time window and context are not verified")
    for name, limits in rules["metrics"].items():
        value = measured.get(name)
        if not number(value):
            missing.append(f"metric missing: {name}")
            continue
        if "max" in limits and value > limits["max"]:
            exceeded.append(f"absolute threshold exceeded: {name}")
        if "max_relative_increase" in limits:
            if not comparable_baseline:
                continue
            base = baseline.get(name)
            if not number(base, minimum=0.000000001):
                missing.append(f"positive comparable baseline missing: {name}")
            elif value > base * (1 + limits["max_relative_increase"]):
                exceeded.append(f"relative threshold exceeded: {name}")
    if exceeded:
        return result("abort", exceeded + missing)
    if missing:
        return result("pause", missing)
    index = rules["stages_pct"].index(stage)
    next_stage = rules["stages_pct"][index + 1] if index + 1 < len(rules["stages_pct"]) else None
    return result("eligible_to_promote" if next_stage is not None else "complete", next_stage_pct=next_stage)


def evaluate(policy, evidence, now=None):
    validate_policy(policy)
    identity = digest(policy)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("evaluation time must include a timezone")
    if (not isinstance(evidence, dict) or evidence.get("schema") != "ci-validation-evidence/1"
            or evidence.get("repository") != policy["repository"]
            or evidence.get("policy_digest") != identity):
        return result("unknown", ["evidence schema, repository or policy identity mismatch"], policy_digest=identity)
    observed = stamp(evidence.get("observed_at"))
    if observed is None or observed > now:
        return result("unknown", ["invalid evidence observation time"], policy_digest=identity)
    if (now - observed).total_seconds() > policy["max_evidence_age_s"]:
        return result("unknown", ["evidence snapshot is stale"], policy_digest=identity)
    record_groups = [evidence.get(key, []) for key in ("pr_events", "integration_records")]
    if isinstance(evidence.get("release_candidate"), dict):
        record_groups.append([evidence["release_candidate"]])
    for group in record_groups:
        for record in group if isinstance(group, list) else []:
            if not isinstance(record, dict):
                continue
            times = [stamp(record.get("received_at")), stamp(record.get("completed_at"))]
            times += [stamp(check.get("completed_at")) for check in record.get("checks", [])
                      if isinstance(check, dict)] if isinstance(record.get("checks"), list) else []
            if isinstance(record.get("provenance"), dict):
                times.append(stamp(record["provenance"].get("verified_at")))
            if any(t is not None and t > observed for t in times):
                return result("unknown", ["event occurs after evidence observation"], policy_digest=identity)
    telemetry = evidence.get("rollout_observation")
    if isinstance(telemetry, dict) and any(
            (t := stamp(telemetry.get(key))) is not None and t > observed
            for key in ("window_started_at", "window_ended_at", "stage_started_at",
                        "baseline_window_started_at", "baseline_window_ended_at")):
        return result("unknown", ["telemetry occurs after evidence observation"], policy_digest=identity)
    deployment = evidence.get("rollout_state")
    if isinstance(deployment, dict):
        stage_start = stamp(deployment.get("stage_started_at"))
        if stage_start is not None and stage_start > observed:
            return result("unknown", ["rollout stage occurs after evidence observation"], policy_digest=identity)
    integration_state = evidence.get("integration_state")
    if isinstance(integration_state, dict):
        active_start = stamp(integration_state.get("active_started_at"))
        if active_start is not None and active_start > observed:
            return result("unknown", ["integration state occurs after evidence observation"], policy_digest=identity)
    # Evaluate all timestamps against the actual evaluation time, not an untrusted
    # self-declared observation time that could keep stale release evidence green.
    health, integrated = integrations(policy, evidence, identity, now)
    release = release_gate(policy, evidence, identity, now, health, integrated)
    return {"schema": "ci-validation-result/1", "policy_digest": identity,
            "evaluated_at": now.isoformat(), "evidence_observed_at": observed.isoformat(),
            "trust_boundary": "Reviewed normalized input; provider authentication, identity resolution and cryptographic verification are external requirements.",
            "pr_budget": pr_budget(policy, evidence, identity, now), "integration": health,
            "release": release, "rollout": rollout_gate(policy, evidence, now, release)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--policy-digest", action="store_true", help="Print canonical policy SHA-256 only")
    parser.add_argument("--now", help="Timezone-aware evaluation time for reproducible offline analysis")
    parser.add_argument("--require", action="append", choices=("pr_budget", "integration", "release", "rollout"),
                        help="Exit 1 unless this requested result passes; repeat for multiple results")
    args = parser.parse_args(argv)
    try:
        policy = json.loads(args.policy.read_text())
        validate_policy(policy)
        if args.policy_digest:
            print(digest(policy))
            return 0
        if args.evidence is None:
            parser.error("--evidence is required unless --policy-digest is used")
        now = stamp(args.now) if args.now else None
        if args.now and now is None:
            parser.error("--now requires a timezone-aware timestamp")
        report = evaluate(policy, json.loads(args.evidence.read_text()), now)
        rendered = json.dumps(report, indent=2, allow_nan=False) + "\n"
        if args.output:
            args.output.write_text(rendered)
        else:
            print(rendered, end="")
        for required in args.require or []:
            accepted = {"eligible_to_promote", "complete", "not_applicable"} if required == "rollout" else {"pass"}
            if report.get(required, {}).get("state") not in accepted:
                return 1
        return 0  # Report creation without --require is never a blanket passing gate.
    except (OSError, ValueError, TypeError) as exc:
        print(f"validation evidence error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
