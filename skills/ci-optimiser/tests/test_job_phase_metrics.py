"""Per-job phase evidence must not conceal missing timing or failed first attempts."""
from __future__ import annotations

import copy
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import analyze_gh as analysis  # noqa: E402


def stamp(seconds):
    return (datetime(2026, 11, 20, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat()


def run(rid=1, attempt=1, **extra):
    return {"id": rid, "name": "ci", "workflow_id": 10, "head_sha": "a" * 40,
            "head_branch": "feature", "event": "pull_request", "run_attempt": attempt,
            "status": "completed", "conclusion": "success", "created_at": stamp(0), **extra}


def step(name, start, end, **extra):
    return {"name": name, "status": "completed", "conclusion": "success",
            "started_at": stamp(start), "completed_at": stamp(end), **extra}


def job(name="tests", queued=0, start=2, end=22, attempt=1, **extra):
    return {"name": name, "status": "completed", "conclusion": "success", "run_attempt": attempt,
            "created_at": stamp(queued), "started_at": stamp(start), "completed_at": stamp(end),
            "labels": ["linux"], "steps": [step("Install dependencies", start, start + 5),
                                           step("Run tests", start + 5, end)], **extra}


class JobPhaseMetricsTest(unittest.TestCase):
    def test_each_job_keeps_its_own_phase_percentiles(self):
        runs = [run(1), run(2)]
        jobs = {1: [job("lint", start=1, end=11), job("tests", start=40, end=100)],
                2: [job("lint", start=2, end=14), job("tests", start=50, end=120)]}
        result = analysis.analyze_group(runs, jobs)["job_phases"]
        self.assertEqual((result["lint"]["waiting_s"]["p50"], result["lint"]["waiting_s"]["p95"]), (1, 2))
        self.assertEqual((result["tests"]["waiting_s"]["p50"], result["tests"]["waiting_s"]["p95"]), (40, 50))
        self.assertEqual((result["tests"]["running_s"]["p50"], result["tests"]["running_s"]["p95"]), (55, 65))
        self.assertEqual(result["tests"]["complete_phase_observations"], 2)
        self.assertEqual(result["tests"]["unknown_by_metric"]["setup_s"], 0)

    def test_missing_install_finish_cannot_look_like_zero_setup(self):
        value = job(start=1, end=101, steps=[step("Install dependencies", 1, 91, completed_at=None),
                                           step("Run tests", 91, 101)])
        result = analysis.job_phase_metrics([run()], {1: [value]})["tests"]
        for key in ("setup_s", "running_s", "unattributed_s", "overlap_s"):
            self.assertEqual(result[key], "unknown")
            self.assertEqual(result["unknown_by_metric"][key], 1)
        self.assertEqual(result["waiting_s"]["p50"], 1)
        self.assertEqual(result["runner_s"]["p50"], 100)
        self.assertEqual(result["complete_phase_observations"], 0)
        self.assertEqual(result["unknown_phase_observations"], 1)

    def test_overlap_and_unattributed_time_are_distinct(self):
        value = job(start=0, end=30, steps=[step("Install dependencies", 0, 15),
                                          step("Run tests", 5, 20)])
        result = analysis.phase_observation(run(), value)
        self.assertEqual((result["setup_s"], result["running_s"], result["overlap_s"],
                          result["unattributed_s"], result["runner_s"]), (15, 15, 10, 10, 30))
        self.assertEqual(result["runner_s"], result["setup_s"] + result["running_s"]
                         - result["overlap_s"] + result["unattributed_s"])
        report = analysis.job_phase_metrics([run()], {1: [value]})["tests"]
        self.assertIn("percentiles are not additive", report["decomposition"])
        self.assertIn("heuristic", report["classification"])

    def test_runner_gaps_are_not_assigned_to_test_execution(self):
        value = job(start=0, end=50, steps=[step("Install dependencies", 5, 10),
                                          step("Run tests", 30, 40)])
        result = analysis.phase_observation(run(), value)
        self.assertEqual((result["setup_s"], result["running_s"], result["unattributed_s"]), (5, 10, 35))

    def test_successful_rerun_keeps_failed_first_attempt(self):
        first = job(start=2, end=22, conclusion="failure",
                    steps=[step("Install dependencies", 2, 7), step("Run tests", 7, 22, conclusion="failure")])
        later = job(queued=30, start=32, end=42, attempt=2)
        result = analysis.analyze_group([run(attempt=2)], {1: [first, later]})["job_phases"]["tests"]
        self.assertEqual(result["observations"], 2)
        self.assertEqual(result["outcomes"], {"failure": 1, "success": 1})
        self.assertEqual(result["attempts"], {"1": 1, "2": 1})
        self.assertEqual(result["first_attempt"]["outcomes"], {"failure": 1})
        self.assertEqual(result["first_attempt"]["running_s"]["p50"], 15)
        self.assertEqual(result["reruns"]["outcomes"], {"success": 1})
        self.assertEqual(result["reruns"]["running_s"]["p50"], 5)
        self.assertEqual(result["by_outcome"]["failure"]["observations"], 1)

    def test_skipped_cancelled_and_incomplete_jobs_are_not_zero_duration_passes(self):
        cases = [job(conclusion="skipped"), job(conclusion="cancelled"), job(conclusion="timed_out"),
                 job(status="in_progress", conclusion=None), job(status=None), job(conclusion={})]
        result = analysis.job_phase_metrics([run()], {1: cases})["tests"]
        self.assertEqual(result["observations"], len(cases))
        self.assertEqual(result["complete_phase_observations"], 0)
        for key in analysis.PHASE_METRICS:
            self.assertEqual(result[key], "unknown")
            self.assertEqual(result["unknown_by_metric"][key], len(cases))
        self.assertEqual(result["outcomes"]["skipped"], 1)
        self.assertEqual(result["outcomes"]["cancelled"], 1)

    def test_bad_step_evidence_invalidates_all_step_phases(self):
        cases = [None, [], {}, [None], [step("Run tests", 2, 22, completed_at=None)],
                 [step("Run tests", 2, 23)], [step("Run tests", 1, 22)],
                 [step("Run tests", 22, 2)], [step("Run tests", 2, 22, conclusion="skipped")],
                 [step("Run tests", 2, 22, status="in_progress")],
                 [step("Run tests", 2, 22, conclusion={})], [step(None, 2, 22)],
                 [step("Run tests", 2, 22, conclusion="failure")]]
        for steps in cases:
            with self.subTest(steps=steps):
                result = analysis.phase_observation(run(), job(steps=steps))
                self.assertIsNone(result["setup_s"])
                self.assertIsNone(result["running_s"])
                self.assertIsNone(result["unattributed_s"])
                self.assertEqual(result["phase_state"], "unknown")
                self.assertTrue(result["gaps"])

    def test_missing_queue_evidence_stays_unknown_without_discarding_known_work(self):
        for created in (None, "bad", stamp(3)):
            with self.subTest(created=created):
                result = analysis.phase_observation(run(), job(created_at=created))
                self.assertIsNone(result["waiting_s"])
                self.assertEqual(result["setup_s"], 5)
                self.assertEqual(result["running_s"], 15)
                self.assertEqual(result["phase_state"], "unknown")

    def test_bad_attempt_and_job_interval_cannot_supply_phases(self):
        for value in (job(attempt=None), job(attempt=True), job(attempt=2), job(name=None),
                      job(started_at=None), job(completed_at=None), job(completed_at=stamp(1))):
            with self.subTest(value=value):
                result = analysis.phase_observation(run(), value)
                self.assertTrue(all(result[key] is None for key in analysis.PHASE_METRICS))
                self.assertEqual(result["phase_state"], "unknown")
        report = analysis.job_phase_metrics([run()], {1: [job(attempt=None)]})["tests"]
        self.assertEqual(report["unknown_attempt"]["observations"], 1)
        self.assertEqual(report["first_attempt"]["waiting_s"], "unknown")

    def test_exact_duplicate_records_are_counted_once(self):
        value = job(id=10)
        report = analysis.job_phase_metrics([run()], {1: [value, copy.deepcopy(value)]})["tests"]
        self.assertEqual(report["observations"], 1)
        self.assertEqual(report["first_attempt"]["observations"], 1)

    def test_conflicting_job_ids_cannot_contribute_timing(self):
        values = [job(id=10), job(id=10, conclusion="failure")]
        report = analysis.job_phase_metrics([run()], {1: values})["tests"]
        self.assertEqual(report["observations"], 2)
        self.assertEqual(report["unknown_phase_observations"], 2)
        self.assertEqual(report["complete_phase_observations"], 0)
        self.assertEqual(report["waiting_s"], "unknown")
        self.assertEqual(report["gaps"]["conflicting records for one job ID"], 2)

    def test_report_is_nested_within_workflow_and_existing_cohorts(self):
        result = analysis.analyze([run()], {1: [job()]}, {})
        self.assertEqual(result["workflows"]["ci [pull_request]"]["job_phases"]["tests"]["observations"], 1)
        self.assertEqual(result["cohorts"][0]["metrics"]["job_phases"]["tests"]["observations"], 1)
        self.assertEqual(result["workflows"]["ci [pull_request]"]["job_duration_s"]["tests"]["p50"], 20)


if __name__ == "__main__":
    unittest.main()
