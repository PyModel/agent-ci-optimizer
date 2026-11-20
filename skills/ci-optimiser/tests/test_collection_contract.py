"""Fresh snapshots, real attempts and complete/unknown API evidence contracts."""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import collect_gh as collector  # noqa: E402


def run(run_id=1, attempt=1, **extra):
    return {"id": run_id, "run_attempt": attempt, "status": "completed", "conclusion": "success",
            "event": "push", "head_branch": "main", "head_sha": f"sha-{run_id}",
            "created_at": "2026-11-17T10:00:00Z", "run_started_at": "2026-11-17T10:00:01Z",
            "updated_at": "2026-11-17T10:01:00Z", **extra}


class FakeApi:
    def __init__(self, runs=None, repo="o/r"):
        self.runs = [run()] if runs is None else runs
        self.repo, self.calls = repo, []
        self.jobs = {}
        self.attempts = {}
        self.rules_pages = {1: []}
        self.classic = {"contexts": ["ci"], "checks": [{"context": "ci", "app_id": 99}]}
        self.fail_run_page = None

    def __call__(self, endpoint):
        self.calls.append(endpoint)
        parts = urlsplit(endpoint)
        path, query = parts.path, parse_qs(parts.query)
        page = int(query.get("page", ["1"])[0])
        if path == f"repos/{self.repo}":
            return {"full_name": self.repo, "id": 10, "default_branch": "main", "private": True}
        if path.endswith("/runs") and "/actions/" in path:
            if self.fail_run_page == page:
                raise collector.GhError("HTTP 500 SENTINEL_SECRET")
            start = (page - 1) * collector.PER_PAGE
            return {"total_count": len(self.runs), "workflow_runs": self.runs[start:start + collector.PER_PAGE]}
        if "/jobs" in path:
            run_id = int(path.split("/runs/")[1].split("/")[0])
            data = self.jobs.get(run_id, [{"id": run_id * 100 + 1, "run_attempt": 1, "steps": []}])
            if isinstance(data, Exception):
                raise data
            if isinstance(data, dict):
                return data
            start = (page - 1) * collector.PER_PAGE
            return {"jobs": data[start:start + collector.PER_PAGE]}
        if "/attempts/" in path:
            run_id = int(path.split("/runs/")[1].split("/")[0])
            attempt = int(path.rsplit("/", 1)[1])
            data = self.attempts.get((run_id, attempt), run(run_id, attempt, conclusion="failure"))
            if isinstance(data, Exception):
                raise data
            return data
        if "/protection/required_status_checks" in path:
            if isinstance(self.classic, Exception):
                raise self.classic
            return self.classic
        if "/rules/branches/" in path:
            data = self.rules_pages.get(page, [])
            if isinstance(data, Exception):
                raise data
            return data
        raise AssertionError(f"unexpected endpoint: {endpoint}")


def current(out):
    pointer = json.loads((out / "current.json").read_text())
    raw = out / pointer["snapshot_path"] / "raw"
    manifest = json.loads((raw / "manifest.json").read_text())
    return pointer, raw, manifest


class SnapshotContractTest(unittest.TestCase):
    def test_refetch_failure_never_uses_previous_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, api = Path(tmp), FakeApi()
            collector.collect(api, "o/r", 10, out)
            old_pointer, old_raw, _ = current(out)
            original = (old_raw / "jobs/1.json").read_bytes()
            api.jobs[1] = collector.GhError("HTTP 500 SENTINEL_SECRET")
            meta = collector.collect(api, "o/r", 10, out)
            pointer, raw, manifest = current(out)
            self.assertNotEqual(pointer["snapshot_id"], old_pointer["snapshot_id"])
            self.assertFalse((raw / "jobs/1.json").exists())
            self.assertEqual(manifest["jobs_payload_run_ids"], [])
            self.assertEqual((old_raw / "jobs/1.json").read_bytes(), original)
            self.assertEqual(meta["runs_with_jobs"], 0)
            self.assertFalse(meta["completeness"]["jobs"])
            self.assertNotIn("SENTINEL_SECRET", json.dumps(meta))
            self.assertFalse((out / "raw").exists())

    def test_failed_later_enumeration_page_leaves_current_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            collector.collect(FakeApi(), "o/r", 10, out)
            before = (out / "current.json").read_bytes()
            api = FakeApi([run(i) for i in range(150)])
            api.fail_run_page = 2
            with self.assertRaises(collector.GhError):
                collector.collect(api, "o/r", 150, out)
            self.assertEqual((out / "current.json").read_bytes(), before)
            self.assertEqual(len(list((out / "snapshots").iterdir())), 1)

    def test_malformed_enumeration_is_not_an_empty_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            api = FakeApi()
            def malformed(path):
                return {"total_count": 5} if "/actions/runs?" in path else api(path)
            with self.assertRaises(collector.GhError):
                collector.collect(malformed, "o/r", 10, out)
            self.assertFalse((out / "current.json").exists())

    def test_duplicate_enumeration_identity_is_not_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with self.assertRaises(collector.GhError):
                collector.collect(FakeApi([run(), run()]), "o/r", 10, out)
            self.assertFalse((out / "current.json").exists())

    def test_write_failure_cleans_staging_and_preserves_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            collector.collect(FakeApi(), "o/r", 10, out)
            before = (out / "current.json").read_bytes()
            writer = collector.write_json
            def fail_jobs(path, data):
                if path.parent.name == "jobs":
                    raise OSError("synthetic write failure")
                return writer(path, data)
            with mock.patch.object(collector, "write_json", fail_jobs), self.assertRaises(OSError):
                collector.collect(FakeApi(), "o/r", 10, out)
            self.assertEqual((out / "current.json").read_bytes(), before)
            self.assertEqual(list((out / ".staging").iterdir()), [])

    def test_manifest_hashes_scope_and_repository_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            collector.collect(FakeApi([run(1), run(2)]), "o/r", 10, out)
            collector.collect(FakeApi([run(7)], repo="other/repo"), "other/repo", 10, out)
            pointer, raw, manifest = current(out)
            self.assertEqual(pointer["repo"], "other/repo")
            self.assertEqual(manifest["repo"], pointer["repo"])
            self.assertEqual(manifest["snapshot_id"], pointer["snapshot_id"])
            self.assertEqual(manifest["selected_run_ids"], [7])
            self.assertEqual(manifest["jobs_payload_run_ids"], [7])
            self.assertEqual(manifest["attempts_payload_run_ids"], [7])
            self.assertFalse((raw / "jobs/1.json").exists())
            for name, info in manifest["files"].items():
                payload = (raw / name).read_bytes()
                self.assertEqual(info, {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})

    def test_run_repository_mismatch_aborts_without_replacing_snapshot(self):
        wrong_identities = [
            {"repository": {"id": 999, "full_name": "other/repo"}},
            {"repository": {"id": 999, "full_name": "o/r"}},
            {"repository": {"id": 10, "full_name": "other/repo"}},
            {"repository": {"id": 10, "full_name": "o/r", "name": "other"}},
            {"repository": {"id": 10, "full_name": "o/r", "owner": {"login": "other"}}},
            {"repository_id": 999},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            collector.collect(FakeApi(), "o/r", 10, out)
            before = (out / "current.json").read_bytes()
            for extra in wrong_identities:
                with self.subTest(extra=extra):
                    api = FakeApi([run(**extra)])
                    with self.assertRaises(collector.GhError) as caught:
                        collector.collect(api, "o/r", 10, out)
                    self.assertEqual(caught.exception.kind, "source_repository_identity_mismatch")
                    self.assertEqual((out / "current.json").read_bytes(), before)
                    self.assertFalse(any("/jobs?" in path for path in api.calls))
            self.assertEqual(len(list((out / "snapshots").iterdir())), 1)

    def test_fork_head_is_allowed_and_owning_repository_identity_is_verified(self):
        owning = {"id": 10, "full_name": "O/R", "name": "R", "owner": {"login": "O"}}
        fork = {"id": 999, "full_name": "contributor/fork"}
        api = FakeApi([run(repository=owning, head_repository=fork, event="pull_request",
                           pull_requests=[{"head": {"repo": fork}, "base": {"ref": "main"}}])])
        api.jobs[1] = [{"id": 101, "run_id": 1, "run_attempt": 1, "head_sha": "sha-1"}]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            meta = collector.collect(api, "o/r", 10, out)
            _, raw, manifest = current(out)
            marker = meta["freshness"]["source_identity"]
            self.assertEqual(marker["schema"], "ci-source-identity/1")
            self.assertEqual(marker["state"], "verified")
            self.assertEqual(marker["expected_repository_id"], 10)
            self.assertEqual(marker["gaps"], [])
            self.assertEqual(marker, manifest["freshness"]["source_identity"])
            self.assertEqual(json.loads((raw / "runs.json").read_text())[0]["head_repository"], fork)

    def test_missing_source_identity_stays_unknown_and_cli_reports_partial_evidence(self):
        api = FakeApi()
        def sparse(path):
            return {"default_branch": "main"} if path == "repos/o/r" else api(path)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            meta = collector.collect(sparse, "o/r", 10, out)
            marker = meta["freshness"]["source_identity"]
            self.assertEqual(marker["state"], "unknown")
            self.assertEqual(marker["expected_repository_id"], "unknown")
            self.assertTrue(any(gap["stage"] == "metadata" for gap in marker["gaps"]))
            self.assertTrue(any(gap["stage"] == "runs" and "repository" in gap["fields"]
                                for gap in marker["gaps"]))
            with mock.patch.object(collector, "gh_api", sparse), redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()):
                self.assertEqual(collector.main(["--repo", "o/r", "--out", tmp]), 3)
            self.assertEqual(json.loads(stdout.getvalue())["source_identity"], "unknown")

    def test_conflicting_job_run_or_source_fields_never_publish_that_payload(self):
        owning = {"id": 10, "full_name": "o/r"}
        cases = [({"run_id": 2}, "job_run_identity_mismatch"),
                 ({"run_id": True}, "job_run_identity_mismatch"),
                 ({"head_sha": "another-commit"}, "source_head_identity_mismatch"),
                 ({"repository": {"id": 999, "full_name": "other/repo"}}, "source_repository_identity_mismatch")]
        for patch, kind in cases:
            with self.subTest(patch=patch), tempfile.TemporaryDirectory() as tmp:
                api, out = FakeApi([run(repository=owning)]), Path(tmp)
                api.jobs[1] = [{"id": 101, "run_id": 1, "run_attempt": 1, "head_sha": "sha-1", **patch}]
                meta = collector.collect(api, "o/r", 10, out)
                _, raw, manifest = current(out)
                self.assertEqual(manifest["jobs_payload_run_ids"], [])
                self.assertFalse((raw / "jobs/1.json").exists())
                self.assertFalse(meta["completeness"]["jobs"])
                self.assertEqual(meta["freshness"]["source_identity"]["state"], "unknown")
                self.assertTrue(any(error["stage"] == "jobs" and error["kind"] == kind for error in meta["errors"]))

    def test_conflicting_prior_attempt_source_is_omitted(self):
        owning = {"id": 10, "full_name": "o/r"}
        cases = [({"repository": {"id": 999, "full_name": "other/repo"}}, "source_repository_identity_mismatch"),
                 ({"head_sha": "another-commit"}, "source_head_identity_mismatch")]
        for patch, kind in cases:
            with self.subTest(patch=patch), tempfile.TemporaryDirectory() as tmp:
                api, out = FakeApi([run(1, 2, repository=owning)]), Path(tmp)
                api.attempts[(1, 1)] = {**run(1, 1, repository=owning), **patch}
                meta = collector.collect(api, "o/r", 10, out)
                _, raw, manifest = current(out)
                attempts = json.loads((raw / "attempts/1.json").read_text())
                self.assertEqual([attempt["run_attempt"] for attempt in attempts], [2])
                self.assertEqual(manifest["attempts_complete_run_ids"], [])
                self.assertFalse(meta["completeness"]["attempts"])
                self.assertTrue(any(error["stage"] == "attempts" and error["kind"] == kind for error in meta["errors"]))

    def test_malformed_repository_metadata_identity_never_publishes(self):
        for patch in ({"id": True}, {"id": 0}, {"full_name": False}):
            with self.subTest(patch=patch), tempfile.TemporaryDirectory() as tmp:
                api = FakeApi()
                def malformed(path):
                    value = api(path)
                    return {**value, **patch} if path == "repos/o/r" else value
                with self.assertRaises(collector.GhError):
                    collector.collect(malformed, "o/r", 10, Path(tmp))
                self.assertFalse((Path(tmp) / "current.json").exists())

    def test_actual_attempts_preserve_first_failure_and_latest_success(self):
        api = FakeApi([run(1, 2)])
        api.jobs[1] = [{"id": 11, "run_attempt": 1, "conclusion": "failure"},
                       {"id": 12, "run_attempt": 2, "conclusion": "success"}]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            meta = collector.collect(api, "o/r", 10, out)
            _, raw, manifest = current(out)
            attempts = json.loads((raw / "attempts/1.json").read_text())
            self.assertEqual([(a["run_attempt"], a["conclusion"]) for a in attempts], [(1, "failure"), (2, "success")])
            self.assertEqual(len(json.loads((raw / "jobs/1.json").read_text())), 2)
            self.assertEqual(manifest["attempts_complete_run_ids"], [1])
            self.assertTrue(meta["completeness"]["attempts"])

    def test_missing_prior_attempt_keeps_available_evidence_and_unknown(self):
        api = FakeApi([run(1, 2)])
        api.attempts[(1, 1)] = collector.GhError("HTTP 404")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            meta = collector.collect(api, "o/r", 10, out)
            _, raw, manifest = current(out)
            self.assertEqual([a["run_attempt"] for a in json.loads((raw / "attempts/1.json").read_text())], [2])
            self.assertFalse(manifest["completeness"]["attempts"])
            self.assertEqual(manifest["attempts_complete_run_ids"], [])
            self.assertTrue(any(e["stage"] == "attempts" and e["status_code"] == 404 for e in meta["errors"]))

    def test_missing_attempt_number_is_not_invented(self):
        api = FakeApi([{"id": 1, "status": "completed"}])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            meta = collector.collect(api, "o/r", 10, out)
            _, raw, _ = current(out)
            self.assertNotIn("run_attempt", json.loads((raw / "attempts/1.json").read_text())[0])
            self.assertFalse(meta["completeness"]["attempts"])

    def test_malformed_jobs_leave_explicit_gap(self):
        api = FakeApi()
        api.jobs[1] = {"not_jobs": []}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            meta = collector.collect(api, "o/r", 10, out)
            _, raw, manifest = current(out)
            self.assertEqual(manifest["jobs_payload_run_ids"], [])
            self.assertFalse((raw / "jobs/1.json").exists())
            self.assertTrue(any(e["kind"] == "malformed_jobs_response" for e in meta["errors"]))

    def test_job_newer_attempt_marks_observation_drift(self):
        api = FakeApi([run(1, 1)])
        api.jobs[1] = [{"id": 10, "run_attempt": 2}]
        with tempfile.TemporaryDirectory() as tmp:
            meta = collector.collect(api, "o/r", 10, Path(tmp))
        self.assertFalse(meta["freshness"]["consistent_observed_attempts"])
        self.assertFalse(meta["freshness"]["point_in_time_transaction"])


class RuleAndWindowContractTest(unittest.TestCase):
    def test_rules_paginate_and_preserve_workflow_and_app_identity_on_auth_404(self):
        api = FakeApi()
        api.classic = collector.GhError("HTTP 404 secret error body")
        api.rules_pages[1] = [{"type": "required_status_checks", "ruleset_id": 9, "parameters": {
            "required_status_checks": [{"context": "ci", "integration_id": 123}]}}] + [{"type": "deletion"}] * 99
        api.rules_pages[2] = [{"type": "workflows", "ruleset_id": 8, "parameters": {
            "workflows": [{"repository_id": 44, "path": ".github/workflows/security.yml", "ref": "refs/heads/main"}]}}]
        errors = []
        required = collector.fetch_required_checks(api, "o/r", "release/v1", errors)
        self.assertEqual(len(required["rulesets"]), 2)
        self.assertEqual(required["source_status"]["rulesets"]["pages"], 2)
        self.assertFalse(required["source_status"]["classic_protection"]["complete"])
        self.assertEqual(required["source_status"]["classic_protection"]["state"], "unknown")
        self.assertEqual(required["classic_protection"]["reason"], "not_found_or_inaccessible")
        self.assertFalse(required["completeness"]["complete"])
        self.assertTrue(any(i.get("app_id") == 123 for i in required["required_identities"]))
        self.assertTrue(any(i.get("repository_id") == 44 for i in required["required_identities"]))
        self.assertTrue(any("/release%2Fv1?" in p for p in api.calls))
        self.assertNotIn("secret error", json.dumps(required))

    def test_later_rules_failure_retains_known_positive_identities(self):
        api = FakeApi()
        api.rules_pages[1] = [{"type": "required_status_checks", "parameters": {
            "required_status_checks": [{"context": "test", "integration_id": 1}]}}] + [{"type": "deletion"}] * 99
        api.rules_pages[2] = collector.GhError("HTTP 403")
        required = collector.fetch_required_checks(api, "o/r", "main", [])
        self.assertEqual(required["source_status"]["rulesets"]["state"], "partial")
        self.assertFalse(required["source_status"]["rulesets"]["complete"])
        self.assertTrue(any(i.get("context") == "test" for i in required["required_identities"]))

    def test_history_branch_and_protected_target_are_separate(self):
        api = FakeApi([run(head_branch="experiment")])
        with tempfile.TemporaryDirectory() as tmp:
            meta = collector.collect(api, "o/r", 10, Path(tmp), branch="experiment", target_branch="release/v2")
        history = next(p for p in api.calls if "/actions/runs?" in p)
        self.assertEqual(parse_qs(urlsplit(history).query)["branch"], ["experiment"])
        self.assertEqual(meta["protected_target_branch"], "release/v2")
        self.assertTrue(any("/branches/release%2Fv2/protection" in p for p in api.calls))
        self.assertFalse(any("/branches/experiment/protection" in p for p in api.calls))
        self.assertEqual(collector.resolve_target_branch([run(head_branch="experiment")], "main", None)[0], "main")

    def test_pr_target_derivation_and_mixed_base_unknown(self):
        a = run(1, event="pull_request", head_branch="feature", pull_requests=[{"base": {"ref": "release"}}])
        b = run(2, event="pull_request", pull_requests=[{"base": {"ref": "main"}}])
        self.assertEqual(collector.resolve_target_branch([a], "main", None)[0], "release")
        self.assertIsNone(collector.resolve_target_branch([a, b], "main", None)[0])
        self.assertIsNone(collector.resolve_target_branch([run(event="pull_request", pull_requests=[])], "main", None)[0])
        with tempfile.TemporaryDirectory() as tmp:
            meta = collector.collect(FakeApi([a, b]), "o/r", 10, Path(tmp))
        self.assertEqual(meta["protected_target_branch"], "unknown")
        self.assertFalse(meta["completeness"]["required_checks"])

    def test_server_filters_and_incomplete_runs_are_retained(self):
        api = FakeApi([run(1, status="waiting", conclusion=None), run(2, status="queued", conclusion=None)])
        enumeration = {}
        rows = collector.fetch_runs(api, "o/r", 10, "feature", "pull_request", created="2026-11-01..2026-11-17",
                                    workflow="ci.yml", include_incomplete=True, enumeration=enumeration)
        request = api.calls[0]
        self.assertIn("/workflows/ci.yml/runs?", request)
        query = parse_qs(urlsplit(request).query)
        self.assertEqual(query["created"], ["2026-11-01..2026-11-17"])
        self.assertEqual(query["branch"], ["feature"])
        self.assertEqual(query["event"], ["pull_request"])
        self.assertNotIn("status", query)
        self.assertEqual([r["status"] for r in rows], ["waiting", "queued"])
        self.assertTrue(enumeration["include_incomplete"])

    def test_limit_and_retention_are_not_full_history_claims(self):
        api = FakeApi([run(i) for i in range(205)])
        enumeration = {}
        rows = collector.fetch_runs(api, "o/r", 150, None, None, enumeration=enumeration)
        self.assertEqual(len(rows), 150)
        self.assertEqual(enumeration["server_total_count"], 205)
        self.assertEqual(enumeration["pages"], 2)
        self.assertTrue(enumeration["limit_reached"])
        self.assertFalse(enumeration["complete"])
        self.assertIn("unknown", enumeration["retention"])
        self.assertEqual(parse_qs(urlsplit(api.calls[0]).query)["status"], ["completed"])

    def test_malformed_classic_does_not_prove_absence(self):
        api = FakeApi()
        api.classic = {}
        required = collector.fetch_required_checks(api, "o/r", "main", [])
        self.assertFalse(required["source_status"]["classic_protection"]["complete"])
        self.assertEqual(required["source_status"]["classic_protection"]["state"], "unknown")

    def test_malformed_nested_gate_records_never_prove_complete_policy(self):
        api = FakeApi()
        api.classic = {"contexts": [], "checks": ["ci"]}
        api.rules_pages[1] = [{"type": "required_status_checks", "parameters": {"required_status_checks": None}},
                              {"type": "workflows", "parameters": {"workflows": [{"ref": "main"}]}}]
        required = collector.fetch_required_checks(api, "o/r", "main", [])
        self.assertFalse(required["source_status"]["classic_protection"]["complete"])
        self.assertFalse(required["source_status"]["rulesets"]["complete"])
        self.assertFalse(required["completeness"]["complete"])


def response(status=200, body=None, headers=None, stderr=""):
    text = json.dumps({} if body is None else body)
    head = f"HTTP/2.0 {status} Response\n"
    head += "".join(f"{key}: {value}\n" for key, value in (headers or {}).items())
    return SimpleNamespace(stdout=head + "\n" + ("" if status == 304 else text),
                           stderr=stderr, returncode=0 if status < 400 else 1)


class TransportContractTest(unittest.TestCase):
    def test_get_version_timeout_and_transient_retries(self):
        runner = mock.Mock(side_effect=[response(503), response(500), response(body={"ok": True})])
        sleep = mock.Mock()
        client = collector.GhClient(runner=runner, sleeper=sleep, timeout=4)
        self.assertEqual(client("repos/o/r"), {"ok": True})
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1.0, 2.0])
        for call in runner.call_args_list:
            command = call.args[0]
            self.assertEqual(command[command.index("--method") + 1], "GET")
            self.assertIn("X-GitHub-Api-Version: 2026-03-10", command)
            self.assertEqual(call.kwargs["timeout"], 4)
        self.assertEqual(client.stats["retries"], 2)

    def test_retry_after_and_primary_reset_are_honored(self):
        for status, headers, expected in ((429, {"Retry-After": "2"}, 2.0),
                                          (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1003"}, 3.0)):
            with self.subTest(status=status):
                sleep = mock.Mock()
                client = collector.GhClient(runner=mock.Mock(side_effect=[response(status, headers=headers), response()]),
                                            sleeper=sleep, clock=lambda: 1000)
                client("repos/o/r")
                sleep.assert_called_once_with(expected)

    def test_long_rate_deadline_defers_instead_of_shortening_it(self):
        runner = mock.Mock(return_value=response(429, headers={"Retry-After": "120"}))
        sleep = mock.Mock()
        client = collector.GhClient(runner=runner, sleeper=sleep, max_wait=5)
        with self.assertRaises(collector.GhError) as caught:
            client("repos/o/r")
        self.assertEqual(caught.exception.retry_after, 120)
        self.assertEqual(caught.exception.kind, "rate_limit")
        runner.assert_called_once()
        sleep.assert_not_called()

    def test_permission_403_not_retried_and_sensitive_stderr_not_exposed(self):
        runner = mock.Mock(return_value=response(403, body={"message": "SYNTHETIC_SERVER_SECRET"},
                                               stderr="SYNTHETIC_CLI_SECRET"))
        with self.assertRaises(collector.GhError) as caught:
            collector.GhClient(runner=runner)("repos/o/r")
        runner.assert_called_once()
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertNotIn("SECRET", json.dumps(caught.exception.record("fetch")))

    def test_timeouts_are_bounded_structured_and_redacted(self):
        runner = mock.Mock(side_effect=subprocess.TimeoutExpired("gh SECRET_COMMAND", 1, stderr="SECRET"))
        sleep = mock.Mock()
        with self.assertRaises(collector.GhError) as caught:
            collector.GhClient(runner=runner, sleeper=sleep, retries=1)("repos/o/r")
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertNotIn("SECRET", str(caught.exception))

    def test_conditional_cache_requires_304_and_never_masks_failed_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = mock.Mock(side_effect=[response(body={"id": 1}, headers={"ETag": '"v1"'}),
                                            response(304), response(500)])
            client = collector.GhClient(cache_dir=Path(tmp), runner=runner, retries=0)
            self.assertEqual(client("repos/o/r"), {"id": 1})
            self.assertEqual(client("repos/o/r"), {"id": 1})
            self.assertIn('If-None-Match: "v1"', runner.call_args_list[1].args[0])
            self.assertEqual(client.stats["conditional_hits"], 1)
            with self.assertRaises(collector.GhError):
                client("repos/o/r")
            self.assertEqual(client.stats["conditional_hits"], 1)

    def test_corrupt_cache_or_different_endpoint_cannot_satisfy_304(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            client = collector.GhClient(cache_dir=cache, runner=mock.Mock(return_value=response(body={"id": 1}, headers={"ETag": '"v1"'})))
            client("repos/o/r")
            path = next(cache.glob("*.json"))
            data = json.loads(path.read_text())
            data["body"] = {"id": 900}
            path.write_text(json.dumps(data))
            client.runner = mock.Mock(return_value=response(304))
            with self.assertRaises(collector.GhError):
                client("repos/o/r")
            self.assertFalse(any("If-None-Match" in arg for arg in client.runner.call_args.args[0]))
            with self.assertRaises(collector.GhError):
                client("repos/other/repo")

    def test_malformed_json_and_missing_cli_are_safe_failures(self):
        bad = SimpleNamespace(stdout="HTTP/2.0 200 OK\n\nNOT JSON SECRET", stderr="", returncode=0)
        for runner, kind in ((mock.Mock(return_value=bad), "malformed_json"),
                             (mock.Mock(side_effect=FileNotFoundError("SECRET")), "gh_not_found")):
            with self.subTest(kind=kind), self.assertRaises(collector.GhError) as caught:
                collector.GhClient(runner=runner)("repos/o/r")
            self.assertEqual(caught.exception.kind, kind)
            self.assertNotIn("SECRET", str(caught.exception))

    def test_cli_partial_exit_and_invalid_transport_options(self):
        api = FakeApi()
        api.jobs[1] = collector.GhError("HTTP 500")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(collector, "gh_api", api), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(collector.main(["--repo", "o/r", "--out", tmp, "--include-incomplete"]), 3)
        for kwargs in ({"timeout": 0}, {"timeout": float("nan")}, {"retries": 6}, {"max_wait": 60}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                collector.GhClient(**kwargs)

    def test_redirect_headers_and_invalid_path(self):
        status, headers, body = collector.parse_http("HTTP/2.0 301 Moved\r\nLocation: /next\r\n\r\n"
                                                     "HTTP/2.0 200 OK\r\nETag: x\r\n\r\n{}")
        self.assertEqual((status, headers["etag"], body), (200, "x", "{}"))
        runner = mock.Mock()
        with self.assertRaises(collector.GhError):
            collector.GhClient(runner=runner)("https://evil.example/token")
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
