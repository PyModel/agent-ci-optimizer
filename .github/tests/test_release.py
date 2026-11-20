"""Release authorization must survive negative controls, not only green fixtures."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("repository_release", ROOT / ".github/release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
SHA = "a" * 40
OTHER = "b" * 40
RUN_ID, WORKFLOW_ID, RELEASE_ID = 71, 81, 91
TAG = "v0.1.1"
REPO = {"id": release.REPOSITORY_ID, "full_name": release.REPOSITORY}
PULLS = "pulls?state=closed&base=main&sort=updated&direction=desc&per_page=100&page="


def run():
    return {"id": RUN_ID, "repository": dict(REPO), "head_repository": dict(REPO),
            "workflow_id": WORKFLOW_ID, "path": release.WORKFLOW_PATH, "name": "ci",
            "event": "push", "head_branch": "main", "head_sha": SHA, "run_attempt": 1,
            "status": "completed", "conclusion": "success"}


def jobs():
    return {"total_count": len(release.REQUIRED_JOBS), "jobs": [
        {"name": name, "run_id": RUN_ID, "run_attempt": 1, "head_sha": SHA,
         "status": "completed", "conclusion": "success",
         "steps": [{"name": step, "status": "completed", "conclusion": "success"}
                   for step in sorted(steps)]}
        for name, steps in release.REQUIRED_JOBS.items()]}


def candidate():
    return {"id": RELEASE_ID, "tag_name": TAG, "target_commitish": SHA,
            "draft": True, "prerelease": False, "assets": []}


def pull(number, *, merged=True, pending=False, sha=SHA):
    return {"number": number, "state": "closed", "base": {"ref": "main", "repo": dict(REPO)},
            "merged_at": "2026-11-20T00:00:00Z" if merged else None, "merge_commit_sha": sha,
            "labels": [{"name": "autorelease: pending"}] if pending else []}


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.values = {
            "": {**REPO, "default_branch": "main"},
            "actions/workflows/ci.yml": {"id": WORKFLOW_ID, "path": release.WORKFLOW_PATH,
                                         "name": "ci", "state": "active"},
            f"actions/runs/{RUN_ID}": run(),
            f"actions/runs/{RUN_ID}/attempts/1/jobs?per_page=100": jobs(),
            "git/ref/heads/main": {"ref": "refs/heads/main", "object": {"type": "commit", "sha": SHA}},
            f"commits/{TAG}": {"sha": SHA},
            f"releases/{RELEASE_ID}": candidate(),
            PULLS + "1": [],
        }
        self.before_call = None

    def __call__(self, path, *, data=None):
        self.calls.append((path, data))
        if self.before_call:
            self.before_call(path, data)
        if data is not None:
            self.values[path] = {**self.values[path], **data}
        return copy.deepcopy(self.values[path])

    @property
    def writes(self):
        return [call for call in self.calls if call[1] is not None]


class CIIdentityTest(unittest.TestCase):
    def test_exact_complete_run_and_jobs_pass(self):
        release.validate_run(run(), SHA, RUN_ID, WORKFLOW_ID)
        release.validate_jobs(jobs(), SHA, RUN_ID)

    def test_only_bound_ref_qualified_run_paths_are_accepted(self):
        for suffix in ("@main", "@refs/heads/main", "@" + SHA):
            release.validate_run({**run(), "path": release.WORKFLOW_PATH + suffix}, SHA, RUN_ID, WORKFLOW_ID)
        for suffix in ("@other", "@" + OTHER, "@main/other"):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                release.validate_run({**run(), "path": release.WORKFLOW_PATH + suffix}, SHA, RUN_ID, WORKFLOW_ID)

    def test_run_identity_outcome_and_retry_mutations_fail(self):
        for key, value in (("id", RUN_ID + 1), ("id", True), ("workflow_id", 9),
                           ("path", ".github/workflows/other.yml"), ("name", "another ci"),
                           ("event", "pull_request"), ("head_branch", "other"),
                           ("head_sha", OTHER), ("run_attempt", 2), ("run_attempt", True),
                           ("status", "in_progress"), ("conclusion", "failure"),
                           ("conclusion", "cancelled"), ("conclusion", "skipped")):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                release.validate_run({**run(), key: value}, SHA, RUN_ID, WORKFLOW_ID)

    def test_same_repository_name_with_different_numeric_identity_fails(self):
        for key in ("repository", "head_repository"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                release.validate_run({**run(), key: {**REPO, "id": REPO["id"] + 1}},
                                     SHA, RUN_ID, WORKFLOW_ID)

    def test_fork_with_matching_sha_cannot_authorize_release(self):
        with self.assertRaises(ValueError):
            release.validate_run({**run(), "head_repository": {**REPO, "full_name": "other/ci-optimiser"}},
                                 SHA, RUN_ID, WORKFLOW_ID)

    def test_missing_extra_duplicate_and_truncated_jobs_fail(self):
        cases = []
        missing = jobs(); missing["jobs"].pop(); missing["total_count"] -= 1; cases.append(missing)
        extra = jobs(); extra["jobs"].append(copy.deepcopy(extra["jobs"][0])); extra["total_count"] += 1; cases.append(extra)
        duplicate = jobs(); duplicate["jobs"][0] = copy.deepcopy(duplicate["jobs"][1]); cases.append(duplicate)
        truncated = jobs(); truncated["total_count"] = 101; cases.append(truncated)
        cases.extend(({}, {"total_count": True, "jobs": []}))
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                release.validate_jobs(value, SHA, RUN_ID)

    def test_green_aggregate_does_not_hide_failed_or_unrun_suite_leg(self):
        for key, value in (("run_id", RUN_ID + 1), ("head_sha", OTHER), ("run_attempt", 2),
                           ("status", "in_progress"), ("conclusion", "skipped"),
                           ("conclusion", "failure"), ("steps", [])):
            data = jobs()
            data["jobs"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                release.validate_jobs(data, SHA, RUN_ID)

    def test_failed_skipped_missing_or_duplicate_step_under_green_job_fails(self):
        for mutation in ("failed", "skipped", "missing", "duplicate"):
            data = jobs()
            steps = data["jobs"][0]["steps"]
            if mutation == "missing":
                steps.pop()
            elif mutation == "duplicate":
                steps.append(copy.deepcopy(steps[0]))
            else:
                steps[0]["conclusion"] = "failure" if mutation == "failed" else "skipped"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                release.validate_jobs(data, SHA, RUN_ID)

    def test_required_job_inventory_matches_repository_budget_contract(self):
        policy = json.loads((ROOT / ".github/ci-budgets.json").read_text())
        self.assertEqual(set(policy["jobs"]), set(release.REQUIRED_JOBS))
        for name, budget in policy["jobs"].items():
            self.assertTrue(set(budget["work_steps"]).issubset(release.REQUIRED_JOBS[name]))


class ReleaseFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "version.txt").write_text("0.1.1\n")
        (self.root / ".release-please-manifest.json").write_text('{".":"0.1.1"}\n')
        (self.root / "release-please-config.json").write_text((ROOT / "release-please-config.json").read_text())
        (self.root / "CHANGELOG.md").write_text("# Changelog\n\n## [0.1.1](https://example.test) (2026-11-20)\n")
        bundle = self.root / "skills/ci-optimiser"
        bundle.mkdir(parents=True)
        (bundle / "SKILL.md").write_text("test fixture")
        (bundle / "LICENSE").write_text("test fixture")
        self.event = {"action": "completed", "repository": dict(REPO), "workflow_run": run()}
        self.event_path = self.root / "event.json"
        self.output = self.root / "output"
        self.env = {"GITHUB_REPOSITORY": release.REPOSITORY,
                    "GITHUB_REPOSITORY_ID": str(release.REPOSITORY_ID),
                    "GITHUB_EVENT_NAME": "workflow_run", "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_EVENT_PATH": str(self.event_path), "GITHUB_OUTPUT": str(self.output)}
        self.api = FakeAPI()

    def invoke(self, mode, *extra):
        self.event_path.write_text(json.dumps(self.event))
        args = [mode, "--sha", SHA, "--run-id", str(RUN_ID)]
        if mode in ("verify", "publish"):
            args += ["--tag", TAG, "--release-sha", SHA]
        if mode == "publish":
            args += ["--release-id", str(RELEASE_ID)]
        self.stderr = io.StringIO()
        with patch.object(release, "ROOT", self.root), patch.object(release, "api", self.api), \
                patch.object(release, "command", return_value=SHA + "\n"), patch.dict("os.environ", self.env), \
                redirect_stderr(self.stderr), redirect_stdout(io.StringIO()):
            return release.main(args + list(extra))

    def test_gate_and_verification_are_read_only(self):
        self.assertEqual(self.invoke("gate"), 0, self.stderr.getvalue())
        self.assertEqual(self.invoke("prepare"), 0, self.stderr.getvalue())
        self.assertEqual(self.invoke("verify"), 0, self.stderr.getvalue())
        self.assertFalse(any(path.startswith("releases/") for path, _ in self.api.calls))
        self.assertEqual(self.api.writes, [])

    def test_pending_release_at_current_main_is_eligible(self):
        self.api.values[PULLS + "1"] = [pull(1, pending=True)]
        self.assertEqual(self.invoke("prepare"), 0, self.stderr.getvalue())
        self.assertEqual(self.api.writes, [])

    def test_older_pending_release_is_blocked_before_release_please_can_tag_it(self):
        self.api.values[PULLS + "1"] = [pull(1), pull(2, pending=True, sha=OTHER)]
        self.assertEqual(self.invoke("prepare"), 1)
        self.assertIn("pending release PR source", self.stderr.getvalue())
        self.assertEqual(self.api.writes, [])

    def test_closed_unmerged_prs_do_not_hide_older_pending_release(self):
        self.api.values[PULLS + "1"] = [pull(i, merged=False) for i in range(1, 101)]
        self.api.values[PULLS + "2"] = [pull(i) for i in range(101, 201)]
        self.api.values[PULLS + "3"] = [pull(i) for i in range(201, 300)] + [pull(300, pending=True, sha=OTHER)]
        self.assertEqual(self.invoke("prepare"), 1)
        self.assertIn("pending release PR source", self.stderr.getvalue())

    def test_full_upstream_200_merged_prefix_is_sufficient(self):
        self.api.values[PULLS + "1"] = [pull(i) for i in range(1, 101)]
        self.api.values[PULLS + "2"] = [pull(i) for i in range(101, 201)]
        self.assertEqual(self.invoke("prepare"), 0, self.stderr.getvalue())
        self.assertNotIn((PULLS + "3", None), self.api.calls)

    def test_unknown_scope_labels_merge_state_and_duplicate_prs_fail(self):
        cases = []
        wrong_base = pull(1); wrong_base["base"]["ref"] = "other"; cases.append([wrong_base])
        missing_merge = pull(1); del missing_merge["merged_at"]; cases.append([missing_merge])
        missing_labels = pull(1); del missing_labels["labels"]; cases.append([missing_labels])
        wrong_repo = pull(1); wrong_repo["base"]["repo"]["id"] += 1; cases.append([wrong_repo])
        cases.extend(([pull(1), pull(1)], [pull(1, sha="")]))
        for value in cases:
            self.api.values[PULLS + "1"] = value
            with self.subTest(value=value):
                self.assertEqual(self.invoke("prepare"), 1)
                self.assertEqual(self.api.writes, [])

    def test_incomplete_candidate_history_never_counts_as_no_candidates(self):
        for page in range(1, 11):
            self.api.values[PULLS + str(page)] = [pull(i, merged=False) for i in range(page * 100, (page + 1) * 100)]
        self.assertEqual(self.invoke("prepare"), 1)
        self.assertIn("completeness bound", self.stderr.getvalue())

    def test_config_cannot_silently_change_the_preflight_candidate_set(self):
        path = self.root / "release-please-config.json"
        original = json.loads(path.read_text())
        for key, value in (("labels", ["different"]), ("plugins", ["group-priority"]),
                           ("packages", {"other": {"release-type": "simple"}}),
                           ("force-tag-creation", False), ("include-v-in-tag", False)):
            path.write_text(json.dumps({**original, key: value}))
            with self.subTest(key=key):
                self.assertEqual(self.invoke("prepare"), 1)
                self.assertEqual(self.api.writes, [])

    def test_publish_has_one_bounded_state_change_after_verification(self):
        self.assertEqual(self.invoke("publish"), 0, self.stderr.getvalue())
        self.assertEqual(self.api.writes, [(f"releases/{RELEASE_ID}", {"draft": False})])
        self.assertEqual(self.api.calls[-1], ("git/ref/heads/main", None))

    def test_old_successful_main_event_fails_without_any_write(self):
        self.api.values["git/ref/heads/main"]["object"]["sha"] = OTHER
        for mode in ("gate", "verify", "publish"):
            with self.subTest(mode=mode):
                self.assertEqual(self.invoke(mode), 1)
                self.assertIn("stale CI event", self.stderr.getvalue())
                self.assertEqual(self.api.writes, [])

    def test_main_advance_immediately_before_publish_blocks_the_patch(self):
        reads = 0
        def move_main(path, data):
            nonlocal reads
            if path == "git/ref/heads/main":
                reads += 1
                if reads == 2:
                    self.api.values[path]["object"]["sha"] = OTHER
        self.api.before_call = move_main
        self.assertEqual(self.invoke("publish"), 1)
        self.assertEqual(self.api.writes, [])

    def test_event_identity_and_api_identity_must_both_agree(self):
        self.event["workflow_run"]["head_sha"] = OTHER
        self.assertEqual(self.invoke("publish"), 1)
        self.assertEqual(self.api.writes, [])
        self.event["workflow_run"]["head_sha"] = SHA
        self.api.values[f"actions/runs/{RUN_ID}"]["workflow_id"] += 1
        self.assertEqual(self.invoke("publish"), 1)
        self.assertEqual(self.api.writes, [])

    def test_retried_release_or_ci_run_never_publishes(self):
        self.env["GITHUB_RUN_ATTEMPT"] = "2"
        self.assertEqual(self.invoke("publish"), 1)
        self.env["GITHUB_RUN_ATTEMPT"] = "1"
        self.api.values[f"actions/runs/{RUN_ID}"]["run_attempt"] = 2
        self.assertEqual(self.invoke("publish"), 1)
        self.assertEqual(self.api.writes, [])

    def test_wrong_workflow_trigger_cannot_publish(self):
        for value in ("pull_request", "pull_request_target", "workflow_dispatch"):
            self.env["GITHUB_EVENT_NAME"] = value
            with self.subTest(value=value):
                self.assertEqual(self.invoke("publish"), 1)
                self.assertEqual(self.api.writes, [])

    def test_release_please_source_mismatch_never_publishes(self):
        self.assertEqual(self.invoke("publish", "--release-sha", OTHER), 1)
        self.assertEqual(self.api.writes, [])

    def test_wrong_tag_target_never_publishes(self):
        self.api.values[f"commits/{TAG}"]["sha"] = OTHER
        self.assertEqual(self.invoke("publish"), 1)
        self.assertEqual(self.api.writes, [])

    def test_tag_move_after_initial_verification_blocks_publication(self):
        reads = 0
        def move_tag(path, data):
            nonlocal reads
            if path == f"commits/{TAG}":
                reads += 1
                if reads == 2:
                    self.api.values[path]["sha"] = OTHER
        self.api.before_call = move_tag
        self.assertEqual(self.invoke("publish"), 1)
        self.assertIn("tag changed before publication", self.stderr.getvalue())
        self.assertEqual(self.api.writes, [])

    def test_asset_source_state_and_release_id_mismatches_never_publish(self):
        for key, value in (("assets", [{"name": "unexpected.zip"}]), ("target_commitish", OTHER),
                           ("draft", False), ("prerelease", True), ("id", RELEASE_ID + 1)):
            self.api.values[f"releases/{RELEASE_ID}"] = {**candidate(), key: value}
            with self.subTest(key=key):
                self.assertEqual(self.invoke("publish"), 1)
                self.assertEqual(self.api.writes, [])

    def test_version_manifest_or_changelog_drift_never_publishes(self):
        for path in ("version.txt", ".release-please-manifest.json", "CHANGELOG.md"):
            target = self.root / path
            original = target.read_text()
            target.write_text(original.replace("0.1.1", "0.1.2"))
            with self.subTest(path=path):
                self.assertEqual(self.invoke("publish"), 1)
                self.assertEqual(self.api.writes, [])
            target.write_text(original)

    def test_protected_initial_or_invalid_version_is_rejected(self):
        for value in ("v0.1.0", "v0.0.9", "v0.01.1", "v0.1.1-beta.1", "main", "../v0.1.1", "v0.1.1\n"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                release.validate_tag(value)

    def test_postpublication_race_is_reported_as_failure_without_false_rollback(self):
        def move_after_patch(path, data):
            if data is not None:
                self.api.values["git/ref/heads/main"]["object"]["sha"] = OTHER
        self.api.before_call = move_after_patch
        self.assertEqual(self.invoke("publish"), 1)
        self.assertEqual(len(self.api.writes), 1)
        self.assertIn("stale CI event", self.stderr.getvalue())
        self.assertIn("Post-publication verification failed", self.stderr.getvalue())

    def test_uncertain_patch_outcome_is_not_reported_as_blocked_publication(self):
        def timeout(path, data):
            if data is not None:
                raise TimeoutError("publisher response unavailable")
        self.api.before_call = timeout
        self.assertEqual(self.invoke("publish"), 1)
        self.assertEqual(len(self.api.writes), 1)
        self.assertIn("Publication may have occurred", self.stderr.getvalue())
        self.assertIn(str(RELEASE_ID), self.stderr.getvalue())
        self.assertNotIn("Release blocked", self.stderr.getvalue())

    def test_http_failure_does_not_retry_or_publish(self):
        def fail(path, data):
            if path == "actions/workflows/ci.yml":
                raise OSError("network unavailable")
        self.api.before_call = fail
        self.assertEqual(self.invoke("publish"), 1)
        self.assertEqual(self.api.calls.count(("actions/workflows/ci.yml", None)), 1)
        self.assertEqual(self.api.writes, [])


class RepositoryContractTest(unittest.TestCase):
    def test_manifest_tracks_version_and_original_release_boundary_is_fixed(self):
        config = json.loads((ROOT / "release-please-config.json").read_text())
        self.assertEqual(config["release-type"], "simple")
        self.assertIs(config["draft"], True)
        self.assertIs(config["force-tag-creation"], True)
        self.assertIs(config["include-component-in-tag"], False)
        self.assertEqual(config["bootstrap-sha"], "d849a540787e37a004a699da0b300b796efd184c")
        self.assertEqual(config["packages"]["."]["package-name"], "ci-optimiser")
        version = (ROOT / "version.txt").read_text().strip()
        self.assertEqual(json.loads((ROOT / ".release-please-manifest.json").read_text()), {".": version})
        self.assertIn("## 0.1.0 (2026-11-20)", (ROOT / "CHANGELOG.md").read_text())

    def test_ci_keeps_full_queue_matrix_and_adds_repository_tests(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        self.assertIn("merge_group:", workflow)
        self.assertIn('python: ["3.10", "3.x"]', workflow)
        self.assertIn("--seconds 60 --label tests -- python ../../.github/run_repository_tests.py", workflow)
        self.assertIn("needs: [test, workflow-lint]", workflow)

    def test_write_jobs_do_not_download_or_execute_pr_artifacts(self):
        workflow = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertIn("permissions: {}", workflow)
        self.assertIn("needs: [version, verify]", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("token: ${{ github.token }}", workflow)
        self.assertIn('.github/release.py prepare --sha "$SOURCE_SHA"', workflow)
        self.assertIn('test "$actual" = "$SOURCE_SHA"', workflow)
        for forbidden in ("pull_request_target:", "workflow_dispatch:", "continue-on-error:",
                          "download-artifact@", "secrets.", "release-type: simple"):
            self.assertNotIn(forbidden, workflow)

    def test_api_rejects_every_unexpected_write(self):
        with patch.object(release, "command") as command:
            for path, data in (("git/refs/heads/main", {"draft": False}),
                               ("releases/1", {"draft": True}), ("releases/1", {"draft": False, "name": "changed"})):
                with self.subTest(path=path, data=data), self.assertRaises(ValueError):
                    release.api(path, data=data)
            command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
