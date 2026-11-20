"""Approval resumes preserve provider attempts and cannot admit executed retries."""
from contextlib import redirect_stderr
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("ci_approval_collector", ROOT / ".github/collect_ci_approval_history.py")
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)
sys.path.insert(0, str(ROOT / "skills/ci-optimiser/tests"))
from test_job_budgets import approval_kwargs, approval_run, budgets, job, payload, policy


def environment():
    return {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_REPOSITORY": "owner/project",
            "GITHUB_REPOSITORY_ID": "50", "GITHUB_RUN_ID": "99", "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_WORKFLOW_REF": "owner/project/.github/workflows/ci.yml@refs/pull/30/merge",
            "EXPECTED_PR_NUMBER": "30", "EXPECTED_API_HEAD_SHA": "a" * 40,
            "GH_TOKEN": "unit-test-token-must-not-appear-in-output"}


class FakeAPI:
    def __init__(self):
        base = "repos/owner/project/actions/runs/99/attempts/"
        current = job(); current["run_attempt"] = 2
        self.responses = {base + "2": approval_run(2, status="in_progress", conclusion=None),
                          base + "1": approval_run(1), base + "1/jobs?per_page=100": payload(),
                          base + "2/jobs?per_page=100": payload(current)}
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        return copy.deepcopy(self.responses[path])


class ApprovalCollectorTest(unittest.TestCase):
    def test_bound_attempt_endpoints_feed_the_unchanged_budget_checks(self):
        api = FakeAPI()
        history, jobs = collector.collect(api, collector.context_from_environment(environment()))
        self.assertEqual(api.calls, list(api.responses))
        options = approval_kwargs(); options["approval_history"] = history
        result = budgets.verify(jobs, policy(), **options)
        self.assertEqual((result["status"], result["attempt"]), ("pass", 2))
        self.assertEqual(history["previous_attempts"][0]["jobs"], {"total_count": 0, "jobs": []})

    def test_authentication_context_is_required_before_any_collection(self):
        changes = {"GITHUB_EVENT_NAME": ["push", "merge_group", "workflow_dispatch"],
                   "GITHUB_REPOSITORY": ["../other", "owner/project/extra"],
                   "GITHUB_REPOSITORY_ID": ["0", "bad"], "GITHUB_RUN_ATTEMPT": ["1", "11", "bad"],
                   "GITHUB_RUN_ID": ["0", "bad"], "GH_TOKEN": [""],
                   "EXPECTED_PR_NUMBER": ["", "0"], "EXPECTED_API_HEAD_SHA": ["main", "a" * 39],
                   "GITHUB_WORKFLOW_REF": ["other/project/.github/workflows/ci.yml@main"]}
        for key, values in changes.items():
            for value in values:
                env = environment(); env[key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    collector.context_from_environment(env)

    def test_prior_failures_and_truncated_pages_are_retained_then_rejected(self):
        for field in ("failure", "nonempty", "incomplete"):
            api = FakeAPI()
            if field == "failure":
                api.responses["repos/owner/project/actions/runs/99/attempts/1"]["conclusion"] = "failure"
            elif field == "nonempty":
                api.responses["repos/owner/project/actions/runs/99/attempts/1/jobs?per_page=100"] = payload(job())
            else:
                api.responses["repos/owner/project/actions/runs/99/attempts/1/jobs?per_page=100"] = {"total_count": 101, "jobs": []}
            history, jobs = collector.collect(api, collector.context_from_environment(environment()))
            options = approval_kwargs(); options["approval_history"] = history
            with self.subTest(field=field), self.assertRaises(ValueError):
                budgets.verify(jobs, policy(), **options)
            self.assertEqual(history["previous_attempts"][0]["run"], api.responses["repos/owner/project/actions/runs/99/attempts/1"])

    def test_current_truncated_inventory_does_not_become_complete(self):
        api = FakeAPI()
        api.responses["repos/owner/project/actions/runs/99/attempts/2/jobs?per_page=100"]["total_count"] = 101
        history, jobs = collector.collect(api, collector.context_from_environment(environment()))
        options = approval_kwargs(); options["approval_history"] = history
        self.assertEqual(budgets.verify(jobs, policy(), **options)["status"], "fail")

    def test_main_uses_no_transport_retry_cache_or_receipt_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = ["--out", str(root / "history.json"), "--jobs-out", str(root / "jobs.json")]
            api = FakeAPI()
            with patch.dict(os.environ, environment(), clear=True), patch.object(collector, "GhClient", return_value=api) as client:
                self.assertEqual(collector.main(args), 0)
                client.assert_called_once_with(timeout=8, retries=0, max_wait=0, cache_dir=None)
            original = (root / "history.json").read_bytes()
            with patch.dict(os.environ, environment(), clear=True), patch.object(collector, "GhClient", return_value=api), redirect_stderr(io.StringIO()):
                self.assertEqual(collector.main(args), 1)
            self.assertEqual((root / "history.json").read_bytes(), original)
            self.assertEqual(json.loads(original)["current_run"]["run_attempt"], 2)

    def test_transport_failure_stays_nonzero_without_retries_or_secret_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); output = io.StringIO()
            api = lambda _: (_ for _ in ()).throw(collector.GhError(kind="http_error", status_code=403))
            with patch.dict(os.environ, environment(), clear=True), patch.object(collector, "GhClient", return_value=api), redirect_stderr(output):
                self.assertEqual(collector.main(["--out", str(root / "history.json"), "--jobs-out", str(root / "jobs.json")]), 1)
            self.assertNotIn(environment()["GH_TOKEN"], output.getvalue())
            self.assertFalse((root / "history.json").exists())

    def test_receipt_bounds_and_nonfinite_data_fail(self):
        with patch.object(collector, "MAX_INPUT_BYTES", 2), self.assertRaises(ValueError):
            collector.encoded({"large": "data"})
        with self.assertRaises(ValueError):
            collector.encoded({"unexpected": float("inf")})


def workflow_script(name):
    lines = (ROOT / ".github/workflows/ci.yml").read_text().splitlines()
    start = lines.index("      - name: " + name)
    start = next(i for i in range(start, len(lines)) if lines[i] == "        run: |") + 1
    body = []
    for line in lines[start:]:
        if line and not line.startswith("          "):
            break
        body.append(line[10:])
    return "\n".join(body)


class ApprovalWorkflowTest(unittest.TestCase):
    def execute(self, name, attempt, event="pull_request"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); log = root / "argv.jsonl"
            stub = root / "python3"
            stub.write_text("#!" + sys.executable + "\nimport json,os,sys\n"
                            "with open(os.environ['ARGV_LOG'],'a') as stream: stream.write(json.dumps(sys.argv[1:])+'\\n')\n")
            stub.chmod(0o755)
            env = {**os.environ, **environment(), "PATH": str(root) + os.pathsep + os.environ["PATH"],
                   "RUNNER_TEMP": str(root), "ARGV_LOG": str(log), "GITHUB_RUN_ATTEMPT": str(attempt),
                   "GITHUB_EVENT_NAME": event}
            result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", workflow_script(name)],
                                    cwd=ROOT, env=env, capture_output=True, text=True, check=False)
            calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            return result, calls

    def test_first_attempt_keeps_original_read_and_verifier_routes(self):
        result, calls = self.execute("Read job timing evidence", 1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[0][1:3], ["--seconds", "10"])
        self.assertIn("gh", calls[0])
        self.assertIn("repos/owner/project/actions/runs/99/jobs?filter=all&per_page=100", calls[0])
        result, calls = self.execute("Enforce job budgets", 1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--approval-history", calls[0])
        self.assertEqual(calls[0][calls[0].index("--attempt") + 1], "1")

    def test_later_pr_uses_collector_and_trusted_context_without_relabelling(self):
        result, calls = self.execute("Read job timing evidence", 2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[0][1:3], ["--seconds", "10"])
        self.assertIn(".github/collect_ci_approval_history.py", calls[0])
        result, calls = self.execute("Enforce job budgets", 2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[0][1:3], ["--seconds", "5"])
        for flag, expected in (("--attempt", "2"), ("--repository", "owner/project"),
                               ("--repository-id", "50"), ("--pull-request", "30"),
                               ("--workflow-ref", environment()["GITHUB_WORKFLOW_REF"])):
            self.assertEqual(calls[0][calls[0].index(flag) + 1], expected)

    def test_later_source_queue_and_dispatch_attempts_fail_before_any_command(self):
        for event in ("push", "merge_group", "workflow_dispatch"):
            for name in ("Read job timing evidence", "Enforce job budgets"):
                with self.subTest(event=event, step=name):
                    result, calls = self.execute(name, 2, event)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
