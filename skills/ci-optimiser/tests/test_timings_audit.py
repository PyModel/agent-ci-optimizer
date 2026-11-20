"""A timing-only audit narrows declared scope, never converts missing evidence to green."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import analyze_gh as analyzer
import collect_gh as collector
import report_gh_timings as reporter
from test_collection_contract import FakeApi, current, run


def verified_api():
    api = FakeApi([run(repository={"id": 10, "full_name": "o/r"})])
    api.jobs[1] = [{"id": 101, "run_id": 1, "run_attempt": 1, "head_sha": "sha-1", "name": "test",
                    "status": "completed", "conclusion": "success", "created_at": "2026-11-17T10:00:00Z",
                    "started_at": "2026-11-17T10:00:01Z", "completed_at": "2026-11-17T10:01:00Z",
                    "steps": [{"name": "Checkout", "number": 1, "status": "completed", "conclusion": "success",
                               "started_at": "2026-11-17T10:00:01Z", "completed_at": "2026-11-17T10:00:03Z"},
                              {"name": "Tests", "number": 2, "status": "completed", "conclusion": "success",
                               "started_at": "2026-11-17T10:00:03Z", "completed_at": "2026-11-17T10:01:00Z"}]}]
    return api


def collect_cli(api, out, *, timings_only=True):
    args = ["--repo", "o/r", "--out", str(out), "--limit", "150", "--include-incomplete",
            "--retries", "0", "--max-rate-wait", "0"]
    if timings_only:
        args.append("--timings-only")
    with mock.patch.object(collector, "gh_api", api), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return collector.main(args)


class TimingScopeTest(unittest.TestCase):
    def test_zero_retry_budget_never_repeats_a_failed_request(self):
        transport = mock.Mock(return_value=SimpleNamespace(
            stdout='HTTP/2.0 503 Service Unavailable\n\n{}', stderr='', returncode=1))
        client = collector.GhClient(retries=0, max_wait=0, timeout=10, runner=transport)
        with self.assertRaises(collector.GhError):
            client("repos/o/r")
        self.assertEqual(transport.call_count, 1)

    def test_timings_only_avoids_admin_endpoints_and_preserves_unknown_protection(self):
        api = verified_api()
        api.classic = collector.GhError(kind="http_error", status_code=403)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            self.assertEqual(collect_cli(api, out), 0)
            _, raw, manifest = current(out)
            required = json.loads((raw / "required_checks.json").read_text())
            self.assertFalse(required["completeness"]["complete"])
            self.assertEqual(required["source_status"]["classic_protection"]["state"], "not_assessed")
            self.assertEqual(manifest["collection_scope"]["name"], "timings-only")
            self.assertFalse(manifest["completeness"]["required_checks"])
            self.assertFalse(any("/protection/" in path or "/rules/" in path for path in api.calls))
            run_endpoint = next(path for path in api.calls if "/actions/runs?" in path)
            self.assertNotIn("status=", run_endpoint)
            metrics = analyzer.analyze(*analyzer.load(out))
            self.assertEqual(metrics["collection_scope"]["protection_security_eligibility"], "not_assessed")
            self.assertEqual(metrics["required_checks"], "unknown")
            report, complete = reporter.render(reporter.project(metrics), "success")
            self.assertTrue(complete)
            self.assertIn("NOT ASSESSED", report)
            self.assertIn("1 / 1", report)

    def test_full_audit_still_fails_inaccessible_protection(self):
        api = verified_api()
        api.classic = collector.GhError(kind="http_error", status_code=403)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(collect_cli(api, Path(tmp), timings_only=False), 3)
            self.assertTrue(any("/protection/" in path for path in api.calls))
            _, _, manifest = current(Path(tmp))
            self.assertEqual(manifest["collection_scope"]["name"], "full")

    def test_timing_errors_and_unknown_identity_remain_nonzero(self):
        for broken in ("jobs", "attempts", "identity", "freshness"):
            with self.subTest(broken=broken), tempfile.TemporaryDirectory() as tmp:
                api = verified_api()
                if broken == "jobs":
                    api.jobs[1] = collector.GhError(kind="http_error", status_code=500)
                elif broken == "attempts":
                    api.runs[0]["run_attempt"] = 2
                    api.attempts[(1, 1)] = collector.GhError(kind="http_error", status_code=404)
                elif broken == "identity":
                    api.runs[0].pop("repository")
                else:
                    api.jobs[1][0]["run_attempt"] = 2
                self.assertEqual(collect_cli(api, Path(tmp)), 3)

    def test_failed_and_incomplete_runs_are_kept(self):
        api = verified_api()
        for status, conclusion in (("completed", "failure"), ("in_progress", None)):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                api.runs[0].update(status=status, conclusion=conclusion)
                self.assertEqual(collect_cli(api, Path(tmp)), 0)
                _, raw, manifest = current(Path(tmp))
                self.assertEqual(json.loads((raw / "runs.json").read_text())[0]["conclusion"], conclusion)
                self.assertTrue(manifest["run_enumeration"]["include_incomplete"])


class TimingReportTest(unittest.TestCase):
    def metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            collector.collect(verified_api(), "o/r", 150, out, timings_only=True, include_incomplete=True)
            return analyzer.analyze(*analyzer.load(out))

    def test_collection_failure_cannot_become_successful_report(self):
        metrics = self.metrics()
        report, complete = reporter.render(reporter.project(metrics), "failure")
        self.assertFalse(complete)
        self.assertIn("FAILED OR INCOMPLETE", report)
        for key in ("jobs", "attempts"):
            with self.subTest(key=key):
                metrics["collection_completeness"][key] = False
                self.assertFalse(reporter.render(reporter.project(metrics), "success")[1])

    def test_markdown_metadata_is_escaped_and_missing_timings_stay_unknown(self):
        metrics = self.metrics()
        metrics["workflows"] = {"![x](https://bad.example)|<script>\n": {"job_phases": {"test": {}}}}
        report, _ = reporter.render(reporter.project(metrics), "success")
        self.assertNotIn("![x]", report)
        self.assertNotIn("<script>", report)
        self.assertIn("unknown / unknown", report)

    def test_cli_publishes_only_bounded_valid_metrics_or_safe_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input.json"
            source.write_text(json.dumps(self.metrics()))
            args = ["--metrics", str(source), "--out", str(root / "out"), "--collection-status", "success"]
            self.assertEqual(reporter.main(args), 0)
            self.assertEqual({p.name for p in (root / "out").iterdir()}, {"report.md", "status.json", "metrics.json"})
            for index, content in enumerate(("SECRET_NOT_JSON", '{"secret":"SENTINEL"}', "[" * 2000,
                                             '{"x":NaN}', '{"x":Infinity}', '{"x":1,"x":2}')):
                source.write_text(content)
                destination = root / f"failure-{index}"
                self.assertEqual(reporter.main(["--metrics", str(source), "--out", str(destination), "--collection-status", "failure"]), 1)
                self.assertFalse((destination / "metrics.json").exists())
                self.assertNotIn("SENTINEL", (destination / "report.md").read_text())
            source.write_bytes(b"x" * 25)
            with mock.patch.object(reporter, "MAX_BYTES", 24):
                self.assertEqual(reporter.main(["--metrics", str(source), "--out", str(root / "oversized"), "--collection-status", "failure"]), 1)
            self.assertEqual(reporter.main(args), 2)

    def test_analyzer_step_commands_and_unknown_fields_never_enter_artifact(self):
        sentinel = "SENTINEL_PRIVATE_COMMAND_LITERAL"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            api = verified_api()
            api.jobs[1][0]["steps"][1]["name"] = "Run curl --header 'Secret: " + sentinel + "'"
            collector.collect(api, "o/r", 150, root / "raw", timings_only=True, include_incomplete=True)
            metrics = analyzer.analyze(*analyzer.load(root / "raw"))
            self.assertIn(sentinel, json.dumps(metrics["workflows"]))
            self.assertIn(sentinel, json.dumps(metrics["cohorts"]))
            metrics["unrecognized_extra"] = sentinel
            source = root / "input.json"
            source.write_text(json.dumps(metrics))
            destination = root / "published"
            self.assertEqual(reporter.main(["--metrics", str(source), "--out", str(destination), "--collection-status", "success"]), 0)
            for path in destination.iterdir():
                self.assertNotIn(sentinel, path.read_text())
            published = json.loads((destination / "metrics.json").read_text())
            self.assertNotIn("cohorts", published)
            self.assertNotIn("unrecognized_extra", published)
            job = next(iter(next(iter(published["workflows"].values()))["job_phases"].values()))
            self.assertEqual(job["name"], "test")
            self.assertEqual(job["running_s"]["p50"], 57)
            self.assertEqual(job["first_attempt"]["observations"], 1)

    def test_oversized_numeric_evidence_is_unknown_without_crashing(self):
        metrics = self.metrics()
        metrics["sample"]["runs"] = 10 ** 400
        projected = reporter.project(metrics)
        self.assertEqual(projected["sample"]["runs"], "unknown")
        self.assertFalse(reporter.render(projected, "success")[1])
        self.assertEqual(reporter.number(10 ** 400), "unknown")


if __name__ == "__main__":
    unittest.main()
