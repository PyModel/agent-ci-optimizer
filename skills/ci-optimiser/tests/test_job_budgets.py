"""Offline job-budget checks must reject gaps, contradictory evidence and reruns."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import check_job_budgets as budgets  # noqa: E402


def stamp(seconds):
    return f"2026-11-20T00:00:{seconds:06.3f}Z"


def step(number, name, start, end):
    return {"number": number, "name": name, "status": "completed", "conclusion": "success",
            "started_at": stamp(start), "completed_at": stamp(end)}


def job(name="test (3.10)", identity=1):
    return {"id": identity, "name": name, "run_id": 99, "head_sha": "a" * 40, "run_attempt": 1,
            "status": "completed", "conclusion": "success", "created_at": stamp(0),
            "started_at": stamp(2), "completed_at": stamp(20), "steps": [
                step(1, "Checkout repository", 2, 3), step(2, "Set up Python", 3, 5),
                step(3, "Test", 6, 10), step(4, "Compile", 11, 13), step(5, "Post checkout", 18, 19)]}


def policy(*names):
    return {"schema": "ci-job-budgets/1", "jobs": {name: {
        "waiting_s": 10, "setup_s": 30, "running_s": 60, "runner_s": 120,
        "setup_end_step": "Set up Python", "work_steps": ["Test", "Compile"]} for name in (names or ("test (3.10)",))}}


def payload(*jobs):
    return {"total_count": len(jobs), "jobs": list(jobs)}


def live_aggregate():
    """The real API lag: checkout is running and every work step is pending."""
    data = job("ci", 4)
    data.update(status="in_progress", conclusion=None, completed_at=None)
    data["steps"] = [step(1, "Checkout repository", 2, 3)]
    data["steps"][0].update(status="in_progress", conclusion=None, completed_at=None)
    for number, name in enumerate(("Require success", "Read job timing evidence", "Enforce job budgets"), 2):
        data["steps"].append({"number": number, "name": name, "status": "pending", "conclusion": None,
                              "started_at": None, "completed_at": None})
    rule = policy("ci")
    rule["jobs"]["ci"].update(setup_end_step="Checkout repository",
                               work_steps=[s["name"] for s in data["steps"][1:]])
    return data, rule


def local_setup():
    return {"schema": "ci-current-job-setup/1", "job": "ci", "run_id": 99, "head_sha": "a" * 40,
            "run_attempt": 1, "setup_end_step": "Checkout repository", "first_work_step": "Require success",
            "observed_at": stamp(6)}


class JobBudgetTest(unittest.TestCase):
    def assert_failure(self, data, rule=None, **kwargs):
        result = budgets.verify(data, rule or policy(), **kwargs)
        self.assertEqual(result["status"], "fail", result)
        self.assertTrue(result["errors"] or any(item["errors"] for item in result["jobs"]))
        return result

    def test_completed_job_splits_intervals_and_excludes_gaps_from_running(self):
        result = budgets.verify(payload(job()), policy())
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["jobs"][0]["measurements"], {
            "waiting_s": 2, "setup_s": 4, "running_s": 6, "runner_s": 18})

    def test_union_does_not_double_count_overlapping_work(self):
        data = job()
        data["steps"][3].update(started_at=stamp(8), completed_at=stamp(13))
        result = budgets.verify(payload(data), policy())
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["jobs"][0]["measurements"]["running_s"], 7)

    def test_equality_fails_each_strict_limit(self):
        for metric, value in (("waiting_s", 2), ("setup_s", 4), ("running_s", 6), ("runner_s", 18)):
            with self.subTest(metric=metric):
                rule = policy()
                rule["jobs"]["test (3.10)"][metric] = value
                result = self.assert_failure(payload(job()), rule)
                self.assertIn(metric, result["jobs"][0]["errors"][0])

    def test_complete_paginated_slurp_and_exact_job_names(self):
        names = ("test (3.10)", "test (3.x)", "workflow-lint", "ci")
        pages = [{"total_count": 4, "jobs": [job(name, i + 1)]} for i, name in enumerate(names)]
        result = budgets.verify(pages, policy(*names))
        self.assertEqual(result["status"], "pass")
        self.assertEqual(len(result["jobs"]), 4)

    def test_incomplete_or_inconsistent_pagination_fails(self):
        for data in ({"total_count": 2, "jobs": [job()]},
                     [{"total_count": 1, "jobs": [job()]}, {"total_count": 2, "jobs": []}]):
            with self.subTest(data=data):
                self.assert_failure(data)

    def test_missing_unexpected_and_duplicate_jobs_fail(self):
        self.assert_failure(payload())
        self.assert_failure(payload(job("renamed")))
        self.assert_failure(payload(job(), job(identity=2)))
        self.assert_failure(payload(job(), job("other", 1)), policy("test (3.10)", "other"))

    def test_mixed_runs_heads_and_missing_provenance_fail(self):
        for key, value in (("run_id", 100), ("head_sha", "b" * 40), ("head_sha", None), ("run_id", True)):
            with self.subTest(key=key, value=value):
                other = job("other", 2)
                other[key] = value
                self.assert_failure(payload(job(), other), policy("test (3.10)", "other"))

    def test_consistent_replayed_jobs_must_match_expected_identity(self):
        data, rule = payload(job(), job("other", 2)), policy("test (3.10)", "other")
        for run_id, head_sha in ((100, "a" * 40), (99, "b" * 40), (100, "b" * 40)):
            with self.subTest(run_id=run_id, head_sha=head_sha):
                self.assert_failure(data, rule, expected_run_id=run_id, expected_head_sha=head_sha)
        result = budgets.verify(data, rule, expected_run_id=99, expected_head_sha="A" * 40)
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["identity_bound"])

    def test_expected_identity_requires_both_valid_fields(self):
        for identity in ({"expected_run_id": 99}, {"expected_head_sha": "a" * 40},
                         {"expected_run_id": 0, "expected_head_sha": "a" * 40},
                         {"expected_run_id": True, "expected_head_sha": "a" * 40},
                         {"expected_run_id": 99, "expected_head_sha": "a" * 39}):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                budgets.verify(payload(job()), policy(), **identity)

    def test_reruns_and_non_success_jobs_fail(self):
        for updates in ({"run_attempt": 2}, {"run_attempt": None}, {"conclusion": "failure"},
                        {"conclusion": "skipped"}, {"conclusion": "cancelled"}, {"status": "in_progress"}):
            with self.subTest(updates=updates):
                data = job()
                data.update(updates)
                self.assert_failure(payload(data))

    def test_missing_or_invalid_timestamps_never_become_zero(self):
        for value in (None, "bad", "2026-11-20T00:00:02", "1960-01-01T00:00:00Z", float("nan"), float("inf")):
            with self.subTest(value=value):
                data = job()
                data["started_at"] = value
                result = self.assert_failure(payload(data))
                self.assertEqual(result["jobs"][0]["measurements"]["waiting_s"], "unknown")

    def test_reversed_and_out_of_job_intervals_fail(self):
        cases = [job() for _ in range(4)]
        cases[0]["created_at"] = stamp(3)
        cases[1]["completed_at"] = stamp(1)
        cases[2]["steps"][2]["completed_at"] = stamp(5)
        cases[3]["steps"][2]["completed_at"] = stamp(21)
        for data in cases:
            self.assert_failure(payload(data))

    def test_missing_marker_work_or_non_success_step_fails(self):
        for change in ("marker", "work", "failed", "skipped", "duplicate", "reordered"):
            with self.subTest(change=change):
                data = job()
                if change == "marker":
                    del data["steps"][1]
                elif change == "work":
                    del data["steps"][2]
                elif change in {"failed", "skipped"}:
                    data["steps"][2]["conclusion"] = change
                elif change == "duplicate":
                    data["steps"][2]["name"] = data["steps"][1]["name"]
                else:
                    data["steps"][2]["number"] = 1
                self.assert_failure(payload(data))

    def test_setup_marker_cannot_overlap_work(self):
        data = job()
        data["steps"][1]["completed_at"] = stamp(7)
        self.assert_failure(payload(data))

    def test_current_job_reports_pending_without_certifying_runtime(self):
        data = job("ci")
        data.update(status="in_progress", conclusion=None, completed_at=None)
        data["steps"][4].update(status="in_progress", conclusion=None, completed_at=None)
        result = budgets.verify(payload(data), policy("ci"), current_job="ci")
        self.assertEqual(result["status"], "pass_with_pending")
        self.assertEqual(result["jobs"][0]["measurements"], {
            "waiting_s": 2, "setup_s": 4, "running_s": "pending", "runner_s": "pending"})
        self.assertEqual(result["jobs"][0]["pending"], ["running_s", "runner_s"])

    def test_current_job_needs_completed_marker_started_work_and_no_failed_steps(self):
        for change in ("marker", "work", "failed", "contradiction"):
            data = job("ci")
            data.update(status="in_progress", conclusion=None, completed_at=None)
            if change == "marker":
                data["steps"][1].update(status="in_progress", conclusion=None, completed_at=None)
            elif change == "work":
                for work in data["steps"][2:]:
                    work.update(status="queued", conclusion=None, started_at=None, completed_at=None)
            elif change == "failed":
                data["steps"][2]["conclusion"] = "failure"
            else:
                data["completed_at"] = stamp(20)
            self.assert_failure(payload(data), policy("ci"), current_job="ci")

    def test_current_job_still_fails_wait_budget(self):
        data = job("ci")
        data.update(status="in_progress", conclusion=None, completed_at=None)
        rule = policy("ci")
        rule["jobs"]["ci"]["waiting_s"] = 2
        self.assert_failure(payload(data), rule, current_job="ci")

    def test_live_pending_steps_are_unstarted_not_successful_work(self):
        for state in ("queued", "pending"):
            with self.subTest(state=state):
                data = job("ci")
                data.update(status="in_progress", conclusion=None, completed_at=None)
                data["steps"][2].update(status="in_progress", conclusion=None, completed_at=None)
                for tail in data["steps"][3:]:
                    tail.update(status=state, conclusion=None, started_at=None, completed_at=None)
                result = budgets.verify(payload(data), policy("ci"), current_job="ci",
                                        expected_run_id=99, expected_head_sha="a" * 40)
                self.assertEqual(result["status"], "pass_with_pending")
                self.assertEqual(result["jobs"][0]["measurements"], {
                    "waiting_s": 2, "setup_s": 4, "running_s": "pending", "runner_s": "pending"})
                self.assertEqual(result["jobs"][0]["step_states"][-1]["status"], state)
                self.assertTrue(result["identity_bound"])

    def test_live_unstarted_step_cannot_have_timing_or_conclusion(self):
        for key, value in (("started_at", stamp(12)), ("completed_at", stamp(13)),
                           ("conclusion", "success"), ("conclusion", "failure")):
            data = job("ci")
            data.update(status="in_progress", conclusion=None, completed_at=None)
            data["steps"][4].update(status="pending", conclusion=None, started_at=None, completed_at=None)
            data["steps"][4][key] = value
            self.assert_failure(payload(data), policy("ci"), current_job="ci")

    def test_live_later_execution_after_unfinished_work_is_not_invented(self):
        for state in ("pending", "queued", "in_progress"):
            data = job("ci")
            data.update(status="in_progress", conclusion=None, completed_at=None)
            data["steps"][2].update(status=state, conclusion=None, completed_at=None)
            if state != "in_progress":
                data["steps"][2]["started_at"] = None
            self.assert_failure(payload(data), policy("ci"), current_job="ci")

    def test_malformed_status_values_fail_without_type_error(self):
        for value in ({}, [], True, 12):
            data = job("ci")
            data["steps"][2]["status"] = value
            self.assert_failure(payload(data), policy("ci"), current_job="ci")

    def test_current_job_cannot_borrow_later_work_start_for_queued_first_work(self):
        data = job("ci")
        data.update(status="in_progress", conclusion=None, completed_at=None)
        data["steps"][2].update(status="queued", conclusion=None, started_at=None, completed_at=None)
        result = self.assert_failure(payload(data), policy("ci"), current_job="ci")
        self.assertTrue(result["jobs"][0]["errors"])

    def test_live_tail_still_rejects_every_observed_non_success(self):
        for conclusion in ("failure", "skipped", "cancelled", "timed_out", "neutral"):
            with self.subTest(conclusion=conclusion):
                data = job("ci")
                data.update(status="in_progress", conclusion=None, completed_at=None)
                data["steps"][2].update(status="in_progress", conclusion=None, completed_at=None)
                data["steps"][3]["conclusion"] = conclusion
                self.assert_failure(payload(data), policy("ci"), current_job="ci")

    def test_settled_jobs_do_not_accept_a_live_step_tail(self):
        for state in ("queued", "pending", "in_progress"):
            data = job("ci")
            data["steps"][2].update(status=state, conclusion=None, completed_at=None)
            if state in ("queued", "pending"):
                data["steps"][2]["started_at"] = None
            self.assert_failure(payload(data), policy("ci"), current_job="ci")

    def test_step_failure_diagnostic_never_contains_names_or_raw_state_values(self):
        data = job("ci")
        data.update(status="in_progress", conclusion=None, completed_at=None)
        secret = "sensitive-shell-command-or-env"
        data["steps"][4].update(name=secret, conclusion=secret)
        result = self.assert_failure(payload(data), policy("ci"), current_job="ci")
        diagnostic = result["jobs"][0]["step_states"][-1]
        self.assertEqual(diagnostic, {"number": 5, "status": "completed", "conclusion": "invalid",
                                      "has_started_at": True, "has_completed_at": True})
        self.assertNotIn(secret, json.dumps(result))

    def test_current_job_missing_completion_fields_is_unknown_not_pending_proof(self):
        data = job("ci")
        data.update(status="in_progress", conclusion=None)
        del data["completed_at"]
        self.assert_failure(payload(data), policy("ci"), current_job="ci")

    def local_verify(self, data, rule, record=None, **kwargs):
        return budgets.verify(payload(data), rule, current_job="ci", expected_run_id=99,
                              expected_head_sha="a" * 40, current_job_setup=record or local_setup(),
                              now=stamp(40), **kwargs)

    def test_local_boundary_handles_stale_checkout_without_rewriting_api_state(self):
        data, rule = live_aggregate()
        original = json.dumps(data, sort_keys=True)
        result = self.local_verify(data, rule)
        self.assertEqual(result["status"], "pass_with_pending", result)
        current = result["jobs"][0]
        self.assertEqual(current["measurements"], {
            "waiting_s": 2, "setup_s": 4, "running_s": "pending", "runner_s": "pending"})
        self.assertEqual(current["setup_evidence"], {
            "source": "local_workflow_boundary", "kind": "conservative_upper_bound"})
        self.assertEqual([s["status"] for s in current["step_states"]], ["in_progress", "pending", "pending", "pending"])
        self.assertEqual(json.dumps(data, sort_keys=True), original)
        self.assert_failure(payload(data), rule, current_job="ci", expected_run_id=99, expected_head_sha="a" * 40)

    def test_local_boundary_keeps_strict_waiting_and_setup_limits(self):
        for metric, value in (("waiting_s", 2), ("setup_s", 4)):
            with self.subTest(metric=metric):
                data, rule = live_aggregate()
                rule["jobs"]["ci"][metric] = value
                result = self.local_verify(data, rule)
                self.assertEqual(result["status"], "fail")
                self.assertIn(metric, result["jobs"][0]["errors"][0])

    def test_local_boundary_needs_exact_identity_and_explicit_bound_current_job(self):
        data, rule = live_aggregate()
        for field, value in (("job", "other"), ("run_id", 100), ("run_id", True),
                             ("head_sha", "b" * 40), ("head_sha", "not-a-sha"),
                             ("run_attempt", 2), ("run_attempt", True)):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.local_verify(data, rule, {**local_setup(), field: value})
        for identity in ({}, {"current_job": "ci"}, {"expected_run_id": 99, "expected_head_sha": "a" * 40}):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                budgets.verify(payload(data), rule, current_job_setup=local_setup(), **identity)
        record = local_setup()
        record["head_sha"] = "A" * 40
        self.assertEqual(self.local_verify(data, rule, record)["status"], "pass_with_pending")

    def test_local_boundary_rejects_missing_unknown_or_invalid_fields(self):
        data, rule = live_aggregate()
        records = [{**local_setup(), "schema": "unknown"}, {**local_setup(), "extra": 1},
                   {**local_setup(), "first_work_step": ""}, {**local_setup(), "observed_at": "bad"},
                   {**local_setup(), "observed_at": "2026-11-20T00:00:06"}]
        for field in local_setup():
            records.append({k: v for k, v in local_setup().items() if k != field})
        for record in records:
            with self.subTest(record=record), self.assertRaises(ValueError):
                self.local_verify(data, rule, record)

    def test_local_boundary_fails_closed_on_clock_disagreement(self):
        data, rule = live_aggregate()
        before_start = {**local_setup(), "observed_at": stamp(1)}
        self.assertEqual(self.local_verify(data, rule, before_start)["status"], "fail")
        with self.assertRaises(ValueError):
            self.local_verify(data, rule, {**local_setup(), "observed_at": stamp(41)})
        # A trustworthy current clock is mandatory; no clamping or slack.
        with self.assertRaises(ValueError):
            budgets.verify(payload(data), rule, current_job="ci", expected_run_id=99,
                           expected_head_sha="a" * 40, current_job_setup=local_setup(), now="bad")

    def test_local_boundary_rejects_wrong_policy_boundaries_and_later_api_times(self):
        for change in ("marker", "first_work", "setup_time", "work_time", "missing_work"):
            with self.subTest(change=change):
                data, rule = live_aggregate()
                record = local_setup()
                if change == "marker":
                    record["setup_end_step"] = "Wrong setup"
                elif change == "first_work":
                    record["first_work_step"] = "Read job timing evidence"
                elif change == "setup_time":
                    data["steps"][0].update(status="completed", conclusion="success", completed_at=stamp(7))
                elif change == "work_time":
                    data["steps"][0].update(status="completed", conclusion="success", completed_at=stamp(3))
                    data["steps"][1].update(status="in_progress", started_at=stamp(7))
                else:
                    data["steps"].pop()
                self.assertEqual(self.local_verify(data, rule, record)["status"], "fail")

    def test_local_boundary_never_rescues_observed_failure_skip_or_contradiction(self):
        for conclusion in ("failure", "skipped", "cancelled", "timed_out", "neutral", "action_required"):
            for index in (0, 1):
                with self.subTest(conclusion=conclusion, index=index):
                    data, rule = live_aggregate()
                    data["steps"][index].update(status="completed", conclusion=conclusion,
                                                 started_at=stamp(3), completed_at=stamp(4))
                    self.assertEqual(self.local_verify(data, rule)["status"], "fail")
        data, rule = live_aggregate()
        data["steps"][1]["started_at"] = stamp(4)  # Pending with a start is still contradictory.
        self.assertEqual(self.local_verify(data, rule)["status"], "fail")

    def test_local_boundary_cannot_rescue_upstream_jobs_or_missing_pages(self):
        for change in ("failed", "unfinished", "step_pending", "missing_page"):
            with self.subTest(change=change):
                data, rule = live_aggregate()
                upstream = job()
                rule["jobs"].update(policy()["jobs"])
                if change == "failed":
                    upstream["conclusion"] = "failure"
                elif change == "unfinished":
                    upstream.update(status="in_progress", conclusion=None, completed_at=None)
                elif change == "step_pending":
                    upstream["steps"][2].update(status="pending", conclusion=None, started_at=None, completed_at=None)
                evidence = payload(upstream, data)
                if change == "missing_page":
                    evidence["total_count"] = 3
                result = budgets.verify(evidence, rule, current_job="ci", expected_run_id=99,
                                        expected_head_sha="a" * 40, current_job_setup=local_setup(), now=stamp(40))
                self.assertEqual(result["status"], "fail")

    def test_local_boundary_does_not_apply_to_completed_current_job(self):
        data, rule = live_aggregate()
        data.update(status="completed", conclusion="success", completed_at=stamp(20))
        self.assertEqual(self.local_verify(data, rule)["status"], "fail")

    def test_local_boundary_cli_reads_record_and_keeps_runtime_pending(self):
        data, rule = live_aggregate()
        with tempfile.TemporaryDirectory() as td:
            paths = [Path(td) / name for name in ("jobs.json", "policy.json", "setup.json")]
            for path, content in zip(paths, (payload(data), rule, local_setup())):
                path.write_text(json.dumps(content))
            output = io.StringIO()
            with redirect_stdout(output):
                rc = budgets.main(["--jobs", str(paths[0]), "--policy", str(paths[1]), "--attempt", "1",
                                   "--run-id", "99", "--head-sha", "a" * 40, "--current-job", "ci",
                                   "--current-job-setup", str(paths[2])])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "pass_with_pending")

    def test_bad_policy_attempt_and_payload_are_invalid_input(self):
        for number in (0, -1, True, float("nan"), float("inf"), 10 ** 400):
            rule = policy()
            rule["jobs"]["test (3.10)"]["waiting_s"] = number
            with self.subTest(number=number), self.assertRaises(ValueError):
                budgets.verify(payload(job()), rule)
        for attempt in (0, 2, True):
            with self.assertRaises(ValueError):
                budgets.verify(payload(job()), policy(), attempt=attempt)
        for data in ([], {"jobs": []}, {"total_count": True, "jobs": []}):
            with self.assertRaises(ValueError):
                budgets.verify(data, policy())
        with self.assertRaises(ValueError):
            budgets.verify(payload(job()), policy(), current_job="unknown")

    def test_cli_exit_codes_and_json_output(self):
        with tempfile.TemporaryDirectory() as td:
            data_path, policy_path = Path(td) / "jobs.json", Path(td) / "policy.json"
            policy_path.write_text(json.dumps(policy()))
            for content, expected_code in ((json.dumps(payload(job())), 0), (json.dumps(payload()), 1), ("{bad", 2)):
                data_path.write_text(content)
                output = io.StringIO()
                with redirect_stdout(output):
                    rc = budgets.main(["--jobs", str(data_path), "--policy", str(policy_path), "--attempt", "1",
                                       "--run-id", "99", "--head-sha", "a" * 40])
                self.assertEqual(rc, expected_code)
                self.assertIsInstance(json.loads(output.getvalue()), dict)

    def test_duplicate_json_keys_fail(self):
        with self.assertRaises(ValueError):
            json.loads('{"jobs":{},"jobs":{}}', object_pairs_hook=budgets.unique_object)

    def test_input_page_and_job_caps(self):
        self.assertEqual(budgets.MAX_INPUT_BYTES, 16 * 1024 * 1024)
        for data in ([{"total_count": 0, "jobs": []}] * (budgets.MAX_PAGES + 1),
                     {"total_count": budgets.MAX_JOBS + 1, "jobs": []},
                     {"total_count": 1, "jobs": [job()] * (budgets.MAX_JOBS + 1)}):
            with self.assertRaises(ValueError):
                budgets.verify(data, policy())

    def test_oversized_duplicate_and_deep_json_have_safe_cli_errors(self):
        secret = "fake-sensitive-content"
        with tempfile.TemporaryDirectory() as td:
            data_path, policy_path = Path(td) / "jobs.json", Path(td) / "policy.json"
            policy_path.write_text(json.dumps(policy()))
            contents = (' ' * (budgets.MAX_INPUT_BYTES + 1), '{"secret":"' + secret + '","secret":0}',
                        '[' * 2000 + '"' + secret + '"' + ']' * 2000)
            for content in contents:
                data_path.write_text(content)
                output = io.StringIO()
                with redirect_stdout(output):
                    rc = budgets.main(["--jobs", str(data_path), "--policy", str(policy_path), "--attempt", "1",
                                       "--run-id", "99", "--head-sha", "a" * 40])
                self.assertEqual(rc, 2)
                self.assertEqual(json.loads(output.getvalue())["status"], "invalid_input")
                self.assertNotIn(secret, output.getvalue())
                self.assertNotIn(str(data_path), output.getvalue())

    def test_reader_never_requests_more_than_bound_plus_one_byte(self):
        stream = io.BytesIO(b'{}')
        with patch.object(Path, "open", return_value=stream), patch.object(stream, "read", wraps=stream.read) as read:
            self.assertEqual(budgets.read_json(Path("unused")), {})
            read.assert_called_once_with(budgets.MAX_INPUT_BYTES + 1)


if __name__ == "__main__":
    unittest.main()
