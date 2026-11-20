"""Regression tests for trustworthy scope, attempt identity, snapshots and graph models."""
from __future__ import annotations

import copy
import hashlib
import io
import itertools
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import analyze_gh as analysis  # noqa: E402
from graph_metrics import evaluate_graph  # noqa: E402


def stamp(seconds: int) -> str:
    return f"2026-11-17T00:{seconds // 60:02d}:{seconds % 60:02d}Z"


def run(rid=1, start=0, end=120, conclusion="success", attempt=1, event="push", branch="main", workflow=10):
    return {"id": rid, "name": "CI", "workflow_id": workflow, "head_sha": "same-sha", "head_branch": branch,
            "event": event, "run_attempt": attempt, "status": "completed", "conclusion": conclusion,
            "created_at": stamp(start), "updated_at": stamp(end), "run_started_at": stamp(start)}


def job(name="test", start=0, end=120, conclusion="success", attempt=1, **extra):
    return {"name": name, "created_at": stamp(start), "started_at": stamp(start), "completed_at": stamp(end),
            "conclusion": conclusion, "run_attempt": attempt, "labels": ["linux"], "steps": [], **extra}


def write(root: Path, relative: str, value) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def snapshot(root: Path, *, job_ids=None, attempt_ids=None):
    sid = "fixture-1"
    base = root / "snapshots" / sid
    pointer = {"schema": "ci-snapshot-pointer/1", "snapshot_id": sid, "snapshot_path": f"snapshots/{sid}", "repo": "o/r"}
    manifest = {"schema": "ci-snapshot-manifest/1", "snapshot_id": sid, "repo": "o/r", "selected_run_ids": [1],
                "jobs_payload_run_ids": [1] if job_ids is None else job_ids,
                "attempts_payload_run_ids": [] if attempt_ids is None else attempt_ids,
                "required_checks_payload": False, "completeness": {"jobs": True, "attempts": True, "required_checks": False}}
    write(root, "current.json", pointer)
    write(base, "raw/manifest.json", manifest)
    write(base, "raw/runs.json", [run()])
    write(base, "raw/collect_meta.json", {"repo": "o/r", "errors": []})
    write(base, "raw/jobs/1.json", [job()])
    return base, pointer, manifest


class SnapshotContractTest(unittest.TestCase):
    def test_source_marker_requires_hashes_and_consistent_actual_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base, _, manifest = snapshot(root, attempt_ids=[1])
            row = {**run(), "repository": {"id": 10, "full_name": "o/r"},
                   "head_repository": {"id": 999, "full_name": "fork/r"}}
            raw_jobs = [job(run_id=1, head_sha=row["head_sha"])]
            marker = {"schema": "ci-source-identity/1", "state": "verified", "expected_repository": "o/r",
                      "expected_repository_id": 10, "checked_records": {"metadata": 1, "runs": 1, "jobs": 1, "attempts": 1}, "gaps": []}
            meta = {"repo": "o/r", "repository_id": 10, "errors": [], "freshness": {"source_identity": marker}}
            manifest["freshness"] = {"source_identity": marker}
            write(base, "raw/runs.json", [row])
            write(base, "raw/jobs/1.json", raw_jobs)
            write(base, "raw/attempts/1.json", [row])
            write(base, "raw/collect_meta.json", meta)
            write(base, "raw/manifest.json", manifest)
            self.assertEqual(analysis.load(root)[2]["_source_identity"]["state"], "unknown")
            def publish_hashes():
                manifest["files"] = {}
                for relative in ("runs.json", "collect_meta.json", "jobs/1.json", "attempts/1.json"):
                    data = (base / "raw" / relative).read_bytes()
                    manifest["files"][relative] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                write(base, "raw/manifest.json", manifest)
            publish_hashes()
            identity = analysis.load(root)[2]["_source_identity"]
            self.assertEqual(identity["state"], "collector_verified")
            self.assertFalse(identity["independently_verified"])
            self.assertEqual(identity["collector_assessment"], marker)
            # A matching rewritten checksum cannot make a contradictory owning
            # identity agree with the snapshot's declared repository.
            write(base, "raw/runs.json", [{**row, "repository": {"id": 999, "full_name": "other/repo"}}])
            publish_hashes()
            with self.assertRaisesRegex(ValueError, "source identity conflict"):
                analysis.load(root)

    def test_source_marker_cannot_hide_missing_or_conflicting_payload_identity(self):
        row = {**run(), "repository": {"id": 10, "full_name": "o/r"}}
        marker = {"schema": "ci-source-identity/1", "state": "verified", "expected_repository": "o/r",
                  "expected_repository_id": 10, "checked_records": {"metadata": 1, "runs": 1, "jobs": 1, "attempts": 1}, "gaps": []}
        meta = {"repo": "o/r", "repository_id": 10, "freshness": {"source_identity": marker}}
        manifest = {"repo": "o/r", "files": {}, "freshness": {"source_identity": marker}}
        missing = analysis.source_identity_evidence(manifest, meta, [row], {1: [job()]}, {1: [row]})
        self.assertEqual(missing["state"], "unknown")
        for field, value in (("head_sha", "other-sha"), ("workflow_id", 999), ("event", "pull_request")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                analysis.source_identity_evidence(manifest, meta, [row], {}, {1: [{**row, field: value}]})
        with self.assertRaisesRegex(ValueError, "source identity conflict"):
            analysis.source_identity_evidence(manifest, meta, [row], {1: [job(run_id=2)]}, {})

    def test_null_or_malformed_integrity_entries_never_disable_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base, _, manifest = snapshot(root)
            inventory = {}
            for relative in ("runs.json", "collect_meta.json", "jobs/1.json"):
                payload = (base / "raw" / relative).read_bytes()
                inventory[relative] = {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
            write(base, "raw/jobs/1.json", [job(end=600)])
            for entry in (None, False, [], {}, {"bytes": True, "sha256": "a" * 64},
                          {"bytes": 1, "sha256": True}):
                with self.subTest(entry=entry):
                    write(base, "raw/manifest.json", {**manifest, "files": {**inventory, "jobs/1.json": entry}})
                    with self.assertRaisesRegex(ValueError, "integrity mismatch"):
                        analysis.load(root)
            write(base, "raw/manifest.json", {**manifest, "files": None})
            with self.assertRaisesRegex(ValueError, "integrity inventory"):
                analysis.load(root)

    def test_manifest_hashes_detect_replaced_same_id_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base, _, manifest = snapshot(root)
            manifest["files"] = {}
            for relative in ("runs.json", "collect_meta.json", "jobs/1.json"):
                payload = (base / "raw" / relative).read_bytes()
                manifest["files"][relative] = {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
            write(base, "raw/manifest.json", manifest)
            self.assertEqual(analysis.load(root)[2]["freshness"], "published_snapshot")
            write(base, "raw/jobs/1.json", [job(end=600)])
            with self.assertRaisesRegex(ValueError, "integrity mismatch"):
                analysis.load(root)

    def test_snapshot_symlink_cannot_escape_input_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "audit"
            outside = Path(directory) / "outside"
            outside.mkdir()
            base, _, _ = snapshot(root)
            (base / "raw/jobs/1.json").unlink()
            write(outside, "other.json", [job()])
            (base / "raw/jobs/1.json").symlink_to(outside / "other.json")
            with self.assertRaisesRegex(ValueError, "escapes input directory"):
                analysis.load(root)

    def test_published_manifest_wins_over_stale_legacy_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base, _, _ = snapshot(root)
            write(root, "raw/runs.json", [run(99)])
            write(root, "raw/jobs/99.json", [job(end=600)])
            write(base, "raw/jobs/88.json", [job(end=600)])
            runs, jobs, meta, required = analysis.load(root)
            self.assertEqual([row["id"] for row in runs], [1])
            self.assertEqual(set(jobs), {1})
            self.assertEqual(meta["freshness"], "published_snapshot")
            self.assertEqual(analysis.analyze(runs, jobs, meta, required)["billable_minutes_by_label"], {"linux": 2})

    def test_unlisted_stale_same_run_payload_is_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot(root, job_ids=[])
            runs, jobs, meta, required = analysis.load(root)
            self.assertEqual(jobs, {})
            metrics = analysis.analyze(runs, jobs, meta, required)
            self.assertEqual(metrics["sample"]["runs_with_jobs"], 0)
            self.assertEqual(metrics["push_to_green_s"], "unknown")
            self.assertEqual(metrics["runner_usage"]["runner_seconds_by_label"], "unknown")

    def test_missing_listed_payload_remains_a_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base, _, _ = snapshot(root)
            (base / "raw/jobs/1.json").unlink()
            _, jobs, meta, _ = analysis.load(root)
            self.assertEqual(jobs, {})
            self.assertIn({"code": "missing_payload", "kind": "jobs", "run_id": 1}, meta["_load_gaps"])

    def test_pointer_escape_or_repository_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base, pointer, manifest = snapshot(root)
            write(root, "current.json", {**pointer, "snapshot_path": "../elsewhere"})
            with self.assertRaises(ValueError):
                analysis.load(root)
            write(root, "current.json", pointer)
            write(base, "raw/manifest.json", {**manifest, "repo": "other/repo"})
            with self.assertRaises(ValueError):
                analysis.load(root)

    def test_manifest_cannot_add_unselected_payloads_or_omit_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base, _, manifest = snapshot(root)
            for patch in ({"jobs_payload_run_ids": [1, 2]}, {"selected_run_ids": []}, {"selected_run_ids": [1, 1]}):
                with self.subTest(patch=patch):
                    write(base, "raw/manifest.json", {**manifest, **patch})
                    with self.assertRaises(ValueError):
                        analysis.load(root)

    def test_attempt_payload_identity_and_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base, _, _ = snapshot(root, attempt_ids=[1])
            write(base, "raw/attempts/1.json", [run()])
            self.assertEqual(analysis.load(root)[2]["_attempts_by_run"][1][0]["id"], 1)
            write(base, "raw/attempts/1.json", [run(2)])
            with self.assertRaises(ValueError):
                analysis.load(root)

    def test_legacy_payloads_are_selected_by_run_id_and_marked_unverified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root, "raw/runs.json", [run()])
            write(root, "raw/jobs/1.json", [job()])
            write(root, "raw/jobs/2.json", [job(end=600)])
            _, jobs, meta, _ = analysis.load(root)
            self.assertEqual(set(jobs), {1})
            self.assertEqual(meta["freshness"], "unverified_legacy")
            self.assertEqual(meta["_load_gaps"][0]["code"], "legacy_freshness_unverified")

    def test_legacy_missing_attempts_load_but_do_not_invent_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_run, old_job = run(), job()
            old_run.pop("run_attempt")
            old_job.pop("run_attempt")
            write(root, "raw/runs.json", [old_run])
            write(root, "raw/jobs/1.json", [old_job])
            runs, jobs, meta, required = analysis.load(root)
            metrics = analysis.analyze(runs, jobs, meta, required)
            self.assertEqual(metrics["freshness"], "unverified_legacy")
            self.assertEqual(metrics["first_pass_failure_rate"], "unknown")
            self.assertEqual(metrics["reliability"]["missing_first_attempt_history"], 1)
            self.assertEqual(metrics["push_to_green_s"], "unknown")
            self.assertEqual(metrics["runner_usage"]["runner_seconds_by_label"], {"linux": 120})


class RunAndAttemptContractTest(unittest.TestCase):
    def test_missing_or_invalid_run_attempt_is_not_attempt_one(self):
        for value in (None, False, True, 0, -1, "1", 1.5, [], {}):
            with self.subTest(value=value):
                row = {**run(), "run_attempt": value}
                metric = analysis.analyze([row], {1: [job()]}, {"_attempts_by_run": {1: [copy.deepcopy(row)]}})
                self.assertEqual(metric["first_pass_failure_rate"], "unknown")
                self.assertEqual(metric["reliability"]["first_attempt_outcomes"], "unknown")
                self.assertEqual(metric["reliability"]["missing_first_attempt_history"], 1)
                self.assertEqual(metric["workflows"]["CI [push]"]["rerun_rate"], "unknown")
                self.assertEqual(metric["workflows"]["CI [push]"]["missing_run_attempt_identity"], 1)
                self.assertEqual(metric["push_to_green_s"], "unknown")
                self.assertIn({"code": "unknown_run_attempt_identity", "run_id": 1}, metric["data_gaps"])

    def test_unknown_attempt_duplicate_cannot_be_hidden_by_known_success(self):
        malformed = {**run(conclusion="failure"), "run_attempt": None}
        for order in itertools.permutations([malformed, run()]):
            metric = analysis.analyze(list(order), {1: [job()]}, {})
            self.assertEqual(metric["push_to_green_s"], "unknown")
            self.assertEqual(metric["first_pass_failure_rate"], "unknown")

    def test_explicit_first_attempt_is_retained_without_inventing_latest_identity(self):
        latest = {**run(attempt=2), "run_attempt": None}
        metric = analysis.analyze([latest], {1: [job()]}, {"_attempts_by_run": {1: [run(conclusion="failure")]}})
        self.assertEqual(metric["first_pass_failure_rate"]["value"], 1)
        self.assertEqual(metric["reliability"]["first_failure_to_success_s"], "unknown")
        self.assertEqual(metric["workflows"]["CI [push]"]["rerun_rate"], "unknown")

    def test_missing_job_attempt_is_not_silently_joined_to_attempt_one(self):
        old_job = job()
        old_job.pop("run_attempt")
        metric = analysis.analyze([run()], {1: [old_job]}, {})
        self.assertEqual(metric["push_to_green_s"], "unknown")
        self.assertEqual(metric["reliability"]["attempt_execution_s"], "unknown")
        self.assertEqual(metric["workflows"]["CI [push]"]["identity_gaps"],
                         [{"run_id": 1, "detail": "job attempt identity absent or invalid"}])

    def test_conflicting_same_run_records_never_select_success_by_array_order(self):
        for alternative in (run(conclusion="failure"), {**run(), "head_sha": "different-sha"}):
            for order in itertools.permutations([run(), alternative]):
                metric = analysis.analyze(list(order), {1: [job()]}, {})
                self.assertEqual(metric["push_to_green_s"], "unknown")
                self.assertIn({"code": "conflicting_run_identity", "run_id": 1}, metric["data_gaps"])

    def test_jobs_from_future_attempt_mark_snapshot_race(self):
        metric = analysis.analyze([run()], {1: [job(), job(start=180, end=240, attempt=2)]}, {})
        self.assertEqual(metric["push_to_green_s"], "unknown")
        self.assertTrue(metric["workflows"]["CI [push]"]["identity_gaps"])

    def test_since_filters_jobs_attempts_and_cost_population(self):
        older, newer = run(1, end=600), run(2, start=660, end=720)
        jobs = {1: [job(end=600)], 2: [job(start=660, end=720)], 99: [job(end=600)]}
        attempts = {1: [older], 2: [newer], 99: [run(99)]}
        metrics = analysis.analyze([older, newer], jobs, {"since": stamp(650), "_attempts_by_run": attempts})
        self.assertEqual(metrics["sample"]["runs"], 1)
        self.assertEqual(metrics["billable_minutes_by_label"], {"linux": 1})
        self.assertEqual(metrics["runner_usage"]["runner_seconds_by_label"], {"linux": 60})
        self.assertEqual(metrics["reliability"]["first_attempt_outcomes"], {"success": 1})

    def test_cli_since_with_nonempty_job_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root, "raw/runs.json", [run(1), run(2, start=660, end=720)])
            write(root, "raw/jobs/1.json", [job(end=600)])
            write(root, "raw/jobs/2.json", [job(start=660, end=720)])
            output = root / "metrics.json"
            with redirect_stdout(io.StringIO()):
                code = analysis.main(["--input", str(root), "--output", str(output), "--since", stamp(650)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.read_text())["billable_minutes_by_label"], {"linux": 1})

    def test_newest_distinct_run_failure_never_uses_older_success(self):
        older, newer = run(1), run(2, start=600, end=720, conclusion="failure")
        jobs = {1: [job()], 2: [job(start=600, end=720, conclusion="failure")]}
        for order in itertools.permutations([older, newer]):
            self.assertEqual(analysis.push_to_green(list(order), jobs), "unknown")

    def test_old_run_high_attempt_does_not_override_newer_run(self):
        older = run(1, attempt=5, conclusion="failure")
        newer = run(2, start=600, end=720)
        jobs = {1: [job(attempt=5, conclusion="failure")], 2: [job(start=600, end=720)]}
        for order in itertools.permutations([older, newer]):
            self.assertEqual(analysis.push_to_green(list(order), jobs)["p50"], 720)

    def test_event_and_branch_contexts_do_not_overwrite_each_other(self):
        runs = [run(1, event="push"), run(2, event="pull_request", conclusion="failure"),
                run(3, branch="feature", start=60, end=180)]
        jobs = {1: [job()], 2: [job(conclusion="failure")], 3: [job(start=60, end=180)]}
        for order in itertools.permutations(runs):
            result = analysis.push_to_green(list(order), jobs)
            self.assertEqual((result["n"], result["p50"]), (2, 120))

    def test_skipped_neutral_incomplete_and_missing_jobs_are_not_green(self):
        for conclusion in ("skipped", "neutral", "cancelled", "failure", None):
            with self.subTest(conclusion=conclusion):
                self.assertEqual(analysis.push_to_green([run(conclusion=conclusion)], {1: [job()]}), "unknown")
        self.assertEqual(analysis.push_to_green([run()], {}), "unknown")
        self.assertEqual(analysis.push_to_green([{**run(), "status": "in_progress"}], {1: [job()]}), "unknown")
        self.assertEqual(analysis.push_to_green([run()], {1: [job(conclusion="skipped")]}), "unknown")

    def test_partial_rerun_retains_completed_predecessor_jobs(self):
        latest = run(attempt=2, end=240)
        jobs = [job("build", end=60), job("test", start=60, end=120, conclusion="failure"),
                job("test", start=180, end=240, attempt=2)]
        effective = analysis.latest_attempt(latest, jobs)
        self.assertEqual({(j["name"], j["run_attempt"]) for j in effective}, {("build", 1), ("test", 2)})
        self.assertEqual(analysis.push_to_green([latest], {1: jobs})["p50"], 240)

    def test_duplicate_job_names_do_not_support_green_proxy(self):
        jobs = [job(id=1), job(id=2)]
        self.assertEqual(analysis.push_to_green([run()], {1: jobs}), "unknown")
        self.assertTrue(analysis.analyze_group([run()], {1: jobs})["identity_gaps"])

    def test_first_attempt_history_and_recovery_are_observed(self):
        latest = run(attempt=2, end=240)
        first = run(conclusion="failure")
        second = {**latest, "run_started_at": stamp(180)}
        jobs = {1: [job(end=120, conclusion="failure"), job(start=180, end=240, attempt=2)]}
        for order in itertools.permutations([first, second]):
            meta = {"_attempts_by_run": {1: list(order)}}
            metric = analysis.analyze([latest], jobs, meta)["reliability"]
            self.assertEqual(metric["first_pass_failure_rate"]["value"], 1)
            self.assertEqual(metric["suspected_retry_recovery_rate"]["value"], 1)
            self.assertEqual(metric["observed_retry_wait_s"]["p50"], 60)
            self.assertEqual(metric["first_failure_to_success_s"]["p50"], 120)
            self.assertNotIn("time_to_green_multiplier", metric["first_pass_failure_rate"])

    def test_successful_benchmark_rerun_is_not_a_failure(self):
        latest = run(attempt=2, end=240)
        attempts = {1: [run(), {**latest, "run_started_at": stamp(180)}]}
        self.assertEqual(analysis.first_pass([latest], attempts)["value"], 0)
        metric = analysis.analyze([latest], {1: [job(), job(start=180, end=240, attempt=2)]}, {"_attempts_by_run": attempts})
        self.assertEqual(metric["reliability"]["successful_repeat_runs"], 1)
        self.assertEqual(metric["flaky_sha_rate"]["value"], 0)

    def test_missing_history_is_unknown_not_a_manufactured_failure(self):
        latest = run(attempt=2)
        self.assertEqual(analysis.first_pass([latest]), "unknown")
        self.assertEqual(analysis.flaky([latest]), "unknown")
        metric = analysis.analyze([latest], {1: [job(attempt=2)]}, {})
        self.assertEqual(metric["reliability"]["missing_first_attempt_history"], 1)

    def test_missing_middle_attempt_cannot_claim_first_recovery_timing(self):
        latest = run(attempt=3, end=360)
        attempts = {1: [run(conclusion="failure"), latest]}
        jobs = {1: [job(conclusion="failure"), job(start=300, end=360, attempt=3)]}
        metric = analysis.analyze([latest], jobs, {"_attempts_by_run": attempts})
        self.assertEqual(metric["reliability"]["first_failure_to_success_s"], "unknown")

    def test_conflicting_attempt_evidence_stays_unknown_in_either_order(self):
        latest = run(attempt=2)
        for order in itertools.permutations([run(), run(conclusion="failure")]):
            self.assertEqual(analysis.first_pass([latest], {1: list(order)}), "unknown")


class RequiredEvidenceContractTest(unittest.TestCase):
    def test_malformed_imported_policy_is_incomplete_even_when_marked_complete(self):
        valid = {"classic_protection": {"contexts": []}, "rulesets": [], "completeness": {"complete": True},
                 "source_status": {key: {"complete": True, "state": "complete"} for key in ("classic_protection", "rulesets")}}
        for classic in ({}, {"contexts": "ci"}, {"contexts": None}, {"checks": ["ci"]}, {"checks": {"context": "ci"}},
                        {"contexts": [None]}, {"checks": [{"context": 3}]}):
            with self.subTest(classic=classic):
                info = analysis.required_info({**valid, "classic_protection": classic})
                self.assertFalse(info["complete"])
                self.assertEqual(info["contexts"], [])
                self.assertTrue(info["gaps"])
        for params in ({}, None, {"required_status_checks": "ci"}, {"required_status_checks": ["ci"]}):
            with self.subTest(params=params):
                info = analysis.required_info({**valid, "rulesets": [{"type": "required_status_checks", "parameters": params}]})
                self.assertFalse(info["complete"])
                self.assertEqual(info["contexts"], [])

    def test_valid_positive_policy_survives_malformed_other_source(self):
        payload = {"classic_protection": {"checks": ["malformed"]}, "rulesets": [
            {"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "test", "integration_id": 7}]}}]}
        result = analysis.required_contexts(payload)
        self.assertEqual(result, {"test"})
        self.assertFalse(result.info["complete"])
        self.assertEqual(result.info["identities"], [{"context": "test", "app_id": 7, "source": "rulesets"}])
        payload = {"classic_protection": {"contexts": ["build"]}, "rulesets": [
            {"type": "required_status_checks", "parameters": {"required_status_checks": ["malformed"]}}]}
        result = analysis.required_contexts(payload)
        self.assertEqual(result, {"build"})
        self.assertFalse(result.info["complete"])

    def test_declared_incomplete_or_malformed_source_status_never_means_empty_complete_policy(self):
        payload = {"classic_protection": {"contexts": []}, "rulesets": []}
        for extra in ({"completeness": {"complete": False}}, {"source_status": []},
                      {"source_status": {"classic_protection": {"complete": True, "state": "unknown"}}}):
            self.assertFalse(analysis.required_info({**payload, **extra})["complete"])

    def test_partial_metadata_keeps_positive_app_bound_identity(self):
        payload = {"classic_protection": {"unavailable": "HTTP 404"}, "rulesets": [
            {"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "test", "integration_id": 7}]}}]}
        required = analysis.required_contexts(payload)
        self.assertEqual(required, {"test"})
        self.assertFalse(required.info["complete"])
        self.assertEqual(required.info["identities"][0]["app_id"], 7)
        group = analysis.analyze_group([run()], {1: [job("other")]}, required)
        self.assertEqual(group["last_finishing_job"]["other"]["required"], "unknown")

    def test_404_alone_never_means_no_required_checks(self):
        self.assertIsNone(analysis.required_contexts({"classic_protection": {"unavailable": "HTTP 404"}, "rulesets": []}))
        self.assertEqual(analysis.required_contexts({"classic_protection": {"contexts": []}, "rulesets": []}), set())

    def test_partial_pagination_and_required_workflows_keep_completeness_false(self):
        payload = {"classic_protection": {"contexts": ["test"]}, "rulesets": [{"type": "workflows", "parameters": {"workflows": [{"path": "check.yml"}]}}],
                   "source_status": {"rulesets": {"complete": False, "state": "partial"}}}
        required = analysis.required_contexts(payload)
        self.assertEqual(required, {"test"})
        self.assertFalse(required.info["complete"])
        self.assertTrue(required.info["required_workflows"])


class StepAndCostContractTest(unittest.TestCase):
    def test_parallel_step_work_is_distinct_from_wall_coverage(self):
        steps = [{"name": "Install dependencies A", "started_at": stamp(0), "completed_at": stamp(10)},
                 {"name": "Install dependencies B", "started_at": stamp(0), "completed_at": stamp(10)},
                 {"name": "Run tests", "started_at": stamp(10), "completed_at": stamp(20)}]
        metric = analysis.analyze_group([run(end=20)], {1: [job(end=20, steps=steps)]})
        self.assertEqual(metric["step_work_s"]["p50"], 30)
        self.assertEqual(metric["step_wall_coverage_s"]["p50"], 20)
        self.assertEqual(metric["setup_work_share"]["p50"], .667)
        self.assertEqual(metric["setup_wall_share"]["p50"], .5)
        self.assertEqual(metric["execution_excluding_setup_coverage_s"]["p50"], 10)
        self.assertEqual(len(metric["step_duration_s"]), 3)

    def test_overlapping_setup_and_execution_never_double_count_wall_time(self):
        steps = [{"name": "Install dependencies", "started_at": stamp(0), "completed_at": stamp(15)},
                 {"name": "Run tests", "started_at": stamp(5), "completed_at": stamp(20)}]
        metric = analysis.step_metrics(job(end=20, steps=steps))
        self.assertEqual((metric["step_work_s"], metric["step_wall_coverage_s"], metric["setup_wall_coverage_s"]), (30, 20, 15))
        self.assertEqual(metric["execution_excluding_setup_coverage_s"], 5)

    def test_missing_step_timings_remain_unknown(self):
        metric = analysis.step_metrics(job(steps=[{"name": "Install", "started_at": stamp(0)}]))
        self.assertIsNone(metric["step_work_s"])
        self.assertIsNone(metric["setup_wall_share"])
        self.assertEqual(metric["missing_step_timings"], 1)

    def test_usage_counts_all_attempts_and_cancelled_work_without_duplicate_ids(self):
        jobs = {1: [job(end=61, id=11), job(end=61, id=11), job(start=120, end=150, attempt=2, conclusion="cancelled", id=12)]}
        metric = analysis.analyze([run(attempt=2, conclusion="cancelled", end=150)], jobs, {})["runner_usage"]
        self.assertEqual(metric["runner_seconds_by_label"], {"linux": 91})
        self.assertEqual(metric["rounded_runner_minutes_estimate_by_label"], {"linux": 3})
        self.assertEqual(metric["cancelled_run_runner_seconds"], 91)
        self.assertEqual(metric["cancelled_job_runner_seconds"], 30)

    def test_explicit_pricing_and_population_scope(self):
        pricing = {"schema": "ci-pricing/1", "source": "user supplied test rate", "as_of": "2026-11-17", "currency": "USD",
                   "rates": [{"labels": ["linux"], "per_minute": .5}], "run_ids": [1], "population_complete": True, "merged_prs": 2}
        result = analysis.analyze([run(end=61)], {1: [job(end=61)]}, {}, pricing=pricing)
        self.assertEqual(result["pricing"]["observed_runner_cost_estimate"], 1)
        self.assertEqual(result["cost_per_merge"], .5)
        wrong_scope = analysis.analyze([run(end=61)], {1: [job(end=61)]}, {}, pricing={**pricing, "run_ids": [1, 2]})
        self.assertEqual(wrong_scope["cost_per_merge"], "unknown")
        missing_rate = analysis.analyze([run()], {1: [job()]}, {}, pricing={**pricing, "rates": []})
        self.assertEqual(missing_rate["pricing"]["observed_runner_cost_estimate"], "unknown")

    def test_rate_samples_and_invalid_timestamps_are_not_duration_evidence(self):
        self.assertTrue(analysis.ratio(1, 20)["low_sample"])
        self.assertNotIn("low_sample", analysis.ratio(1, 30))
        self.assertEqual(analysis.dist(list(range(1, 11)))["p95_tail_observations"], 1)
        self.assertIsNone(analysis.secs("2026-11-17T00:00:00", stamp(60)))
        self.assertEqual(analysis.dist([None, float("nan"), float("inf")]), "unknown")

    def test_cohorts_preserve_trust_runner_cache_config_and_workload(self):
        context = {"schema": "ci-cohort-context/1", "runs": {
            "1": {"trust": "fork", "config": "a", "cache": "cold", "workload": "unit", "runner_class": "small"},
            "2": {"trust": "trusted", "config": "b", "cache": "warm", "workload": "integration", "runner_class": "large"}}}
        metric = analysis.analyze([run(1), run(2)], {1: [job()], 2: [job(labels=["arm"])]}, {}, cohort_context=context)
        self.assertEqual(len(metric["cohorts"]), 2)
        self.assertTrue(all(not cohort["missing_attributes"] for cohort in metric["cohorts"]))
        self.assertEqual({cohort["identity"]["trust"] for cohort in metric["cohorts"]}, {"fork", "trusted"})
        with self.assertRaises(ValueError):
            analysis.apply_cohort_context([run()], {"schema": "ci-cohort-context/1", "runs": {"1": {"unexpected": "x"}}})

    def test_same_name_different_workflow_ids_do_not_merge(self):
        metric = analysis.analyze([run(1, workflow=10), run(2, workflow=11)], {1: [job()], 2: [job()]}, {})
        self.assertEqual(len(metric["workflows"]), 2)


class GraphContractTest(unittest.TestCase):
    def graph(self):
        return {"schema": "ci-job-graph/1", "complete": True, "matrix_resolved": True, "required_nodes": ["gate"],
                "nodes": [{"id": "build", "needs": [], "duration_s": 300},
                          {"id": "test", "needs": ["build"], "duration_s": 5},
                          {"id": "lint", "needs": [], "duration_s": 120},
                          {"id": "gate", "needs": ["test", "lint"], "duration_s": 1}]}

    def test_chain_and_aggregate_recompute_upstream_savings(self):
        graph = self.graph()
        graph["counterfactuals"] = [{"name": "faster build", "durations_s": {"build": 200}}]
        result = evaluate_graph(graph)
        self.assertEqual(result["required_completion_s"], 306)
        candidate = result["counterfactuals"][0]
        self.assertEqual(candidate["required_completion_s"], 206)
        self.assertEqual(candidate["required_path_savings_s"], 100)
        self.assertEqual(candidate["critical_path"], ["build", "test", "gate"])

    def test_diamond_combined_savings_are_recomputed_not_added(self):
        graph = self.graph()
        graph["nodes"] = [{"id": "a", "needs": [], "duration_s": 300},
                          {"id": "b", "needs": [], "duration_s": 299},
                          {"id": "gate", "needs": ["a", "b"], "duration_s": 1}]
        graph["counterfactuals"] = [{"name": "a", "durations_s": {"a": 200}},
                                    {"name": "b", "durations_s": {"b": 199}},
                                    {"name": "both", "durations_s": {"a": 200, "b": 199}}]
        scenarios = evaluate_graph(graph)["counterfactuals"]
        self.assertEqual([s["required_path_savings_s"] for s in scenarios], [1, 0, 100])
        self.assertEqual(scenarios[0]["critical_path"], ["b", "gate"])

    def test_cycles_missing_nodes_unknown_durations_and_unresolved_matrices_are_unknown(self):
        fixtures = []
        graph = self.graph(); graph["nodes"][0]["needs"] = ["gate"]; fixtures.append(graph)
        graph = self.graph(); graph["nodes"][0]["needs"] = ["missing"]; fixtures.append(graph)
        graph = self.graph(); graph["nodes"][0]["duration_s"] = None; fixtures.append(graph)
        graph = self.graph(); graph["matrix_resolved"] = False; fixtures.append(graph)
        graph = self.graph(); graph["complete"] = False; fixtures.append(graph)
        graph = self.graph(); graph["required_nodes"] = []; fixtures.append(graph)
        for graph in fixtures:
            with self.subTest(graph=graph):
                result = evaluate_graph(graph)
                self.assertEqual((result["status"], result["required_completion_s"]), ("unknown", "unknown"))
                self.assertTrue(result["gaps"])

    def test_invalid_counterfactual_cannot_produce_a_numeric_saving(self):
        graph = self.graph()
        graph["counterfactuals"] = [{"name": "unknown node", "durations_s": {"missing": 1}},
                                    {"name": "negative", "durations_s": {"build": -1}}]
        result = evaluate_graph(graph)
        self.assertEqual(result["status"], "modeled")
        self.assertTrue(all(row["status"] == "unknown" for row in result["counterfactuals"]))


if __name__ == "__main__":
    unittest.main()
