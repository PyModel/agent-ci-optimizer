"""Static workflow interpretation must preserve unknowns and caller boundaries."""
from __future__ import annotations

import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import inspect_workflows as inspector  # noqa: E402

PATH = ".github/workflows/ci.yml"
SHA = "a" * 40
FEATURES = {name: True for name in inspector.FEATURES}
CONTEXT = {"platform": "ghec", "host": "github.com", "trigger_cache_default": "write", "features": FEATURES}


def workflow(*steps, **extra):
    return {"on": "pull_request", "jobs": {"test": {"runs-on": "ubuntu-latest", "steps": list(steps)}}, **extra}


def lock(ref="actions/checkout@v7", paths=None):
    return {"version": "v0.0.3", "workflows": {PATH: [ref]} if paths is None else paths,
            "dependencies": {ref: {"ref": ref.rsplit("@", 1)[1], "commit": "sha1-" + SHA, "owner_id": 10, "repo_id": 20}}}


def inspect(value, locked=None, context=None):
    return inspector.inspect_workflow(value, PATH, locked, CONTEXT if context is None else context)


def codes(result):
    return {entry["code"] for entry in result["findings"]}


class ReferencesTest(unittest.TestCase):
    def test_full_sha_and_docker_digest_are_distinct_from_mutable_refs(self):
        result = inspect(workflow({"uses": "owner/action@" + SHA}, {"uses": "docker://image@sha256:" + "b" * 64},
                                  {"uses": "docker://image:latest"}, {"uses": "owner/action@main"}))
        self.assertEqual([a["classification"] for a in result["action_references"]],
                         ["direct_full_sha", "docker_digest", "mutable", "mutable"])
        self.assertFalse(any(a["live_verified"] for a in result["action_references"]))

    def test_lockfile_coverage_normalizes_owner_case_and_action_subpath(self):
        result = inspect(workflow({"uses": "Actions/Checkout/subdir@v7"}), lock())
        action = result["action_references"][0]
        self.assertEqual(action["classification"], "lockfile_covered")
        self.assertEqual(action["commit"], SHA)
        self.assertTrue(action["lockfile_covered"])
        self.assertFalse(result["has_errors"])
        self.assertFalse(result["lockfile"]["reachability_verified"])
        self.assertFalse(result["lockfile"]["repository_identity_verified"])
        self.assertEqual(result["lockfile"]["transitive_coverage"], "unknown")

    def test_a_dependency_under_another_workflow_does_not_cover_this_one(self):
        locked = lock(paths={PATH: [], ".github/workflows/other.yml": ["actions/checkout@v7"]})
        result = inspect(workflow({"uses": "actions/checkout@v7"}), locked)
        self.assertIn("lockfile_missing_action", codes(result))
        self.assertFalse(result["action_references"][0]["lockfile_covered"])

    def test_job_reusable_call_is_never_covered_by_caller_action_lockfile(self):
        ref = "owner/pipelines@main"
        value = {"on": "push", "jobs": {"call": {"uses": "owner/pipelines/.github/workflows/build.yml@main"}}}
        result = inspect(value, lock(ref))
        action = result["action_references"][0]
        self.assertEqual(action["classification"], "mutable")
        self.assertFalse(action["lockfile_covered"])
        self.assertIn("reusable_not_expanded", codes(result))
        self.assertEqual(result["jobs"][0]["reusable_expansion"], "unknown")

    def test_reusable_paths_require_one_yaml_basename_and_local_forms_have_no_ref(self):
        for ref in ("./.github/workflows/build.yml@main", "./.github/workflows/sub/build.yml",
                    "./.github/workflows/build.txt", "$/.github/workflows/sub/build.yaml",
                    "owner/repo/.github/workflows/sub/build.yml@main", "owner/repo/.github/workflows/build.txt@" + SHA):
            with self.subTest(ref=ref):
                result = inspect({"on": "push", "jobs": {"call": {"uses": ref}}})
                self.assertIn("invalid_reusable_path", codes(result))
                item = result["action_references"][0]
                self.assertEqual(item["classification"], "unknown")
                self.assertNotIn("resolution", item)
        for ref in ("./.github/workflows/build.yaml", "$/.github/workflows/build.yml", "owner/repo/.github/workflows/build.yml@" + SHA):
            with self.subTest(valid_ref=ref):
                self.assertNotIn("invalid_reusable_path", codes(inspect({"on": "push", "jobs": {"call": {"uses": ref}}})))

    def test_local_action_and_same_repo_refs_preserve_different_resolution(self):
        result = inspect(workflow({"uses": "./.github/actions/build"}, {"uses": "$/.github/actions/build"}))
        local, same = result["action_references"]
        self.assertEqual((local["classification"], same["classification"]), ("local_path", "same_repo"))
        self.assertIn("workspace", local["resolution"])
        self.assertIn("running workflow/action commit", same["resolution"])
        reusable = inspect({"on": "push", "jobs": {"call": {"uses": "./.github/workflows/build.yml"}}})
        self.assertEqual(reusable["action_references"][0]["resolution"], "same workflow commit")

    def test_same_repo_suffix_and_ghes_availability_are_rejected(self):
        self.assertIn("invalid_same_repo_ref", codes(inspect(workflow({"uses": "$/.github/actions/build@main"}))))
        result = inspect(workflow({"uses": "$/.github/actions/build"}), context={**CONTEXT, "platform": "ghes"})
        self.assertIn("feature_unavailable", codes(result))
        self.assertFalse(result["action_references"][0]["feature_available"])

    def test_feature_availability_is_not_inferred_for_public_github_syntax(self):
        result = inspect(workflow({"uses": "actions/checkout@v7"}), lock(), {"platform": "github.com"})
        self.assertEqual(result["action_references"][0]["feature_available"], "unknown")
        self.assertIn("feature_availability", codes(result))

    def test_expression_ref_is_unknown_and_expression_body_not_echoed(self):
        result = inspect(workflow({"uses": "${{ inputs.SENSITIVE_LOOKING_VALUE }}"}))
        self.assertEqual(result["action_references"][0]["classification"], "unknown")
        self.assertNotIn("SENSITIVE_LOOKING_VALUE", json.dumps(result))


class LockFormatTest(unittest.TestCase):
    def test_non_object_and_missing_version_are_invalid_not_covered(self):
        for locked in ([], True, {}, {"version": 3}):
            with self.subTest(locked=locked):
                result = inspect(workflow({"uses": "actions/checkout@v7"}), locked)
                self.assertFalse(result["lockfile"]["format_valid"])
                self.assertIn("lockfile_format", codes(result))

    def test_malformed_commit_ids_or_ref_never_establish_coverage(self):
        for field, value in (("commit", "sha1-short"), ("owner_id", True), ("repo_id", 0), ("ref", "another")):
            with self.subTest(field=field):
                locked = lock()
                locked["dependencies"]["actions/checkout@v7"][field] = value
                result = inspect(workflow({"uses": "actions/checkout@v7"}), locked)
                self.assertFalse(result["lockfile"]["format_valid"])
                self.assertFalse(result["action_references"][0]["lockfile_covered"])
                self.assertIn("lockfile_format", codes(result))

    def test_missing_transitive_pin_in_flat_workflow_list_is_invalid(self):
        locked = lock()
        locked["dependencies"]["actions/checkout@v7"]["uses"] = ["owner/child@v1"]
        locked["dependencies"]["owner/child@v1"] = {"ref": "v1", "commit": "sha1-" + SHA, "owner_id": 3, "repo_id": 4}
        result = inspect(workflow({"uses": "actions/checkout@v7"}), locked)
        self.assertIn("lockfile_format", codes(result))
        self.assertFalse(result["lockfile"]["format_valid"])

    def test_unknown_version_is_explicitly_unverified(self):
        locked = {**lock(), "version": "v9.0.0"}
        result = inspect(workflow({"uses": "actions/checkout@v7"}), locked)
        self.assertEqual(result["lockfile"]["format_valid"], "unknown")
        self.assertIn("lockfile_version", codes(result))
        self.assertEqual(result["action_references"][0]["classification"], "mutable")

    def test_tenant_hostname_cannot_be_assumed_to_mean_github_com(self):
        locked = lock()
        locked["dependencies"]["actions/checkout@v7"]["hostname"] = "tenant.ghe.com"
        result = inspect(workflow({"uses": "actions/checkout@v7"}), locked)
        self.assertIn("lockfile_format", codes(result))


class CacheSemanticsTest(unittest.TestCase):
    def test_reusable_workflow_with_unseen_caller_does_not_assume_unbounded_access(self):
        value = workflow({"run": "test"}, **{"on": "workflow_call", "cache-mode": "write"})
        unresolved = inspect(value)
        self.assertEqual(unresolved["jobs"][0]["cache"]["effective"], "unknown")
        self.assertIn("caller_context_missing", codes(unresolved))
        no_limit = inspect(value, context={**CONTEXT, "caller_cache_limit": None})
        self.assertEqual(no_limit["jobs"][0]["cache"]["effective"], "write")
        zero_access = inspect(value, context={**CONTEXT, "caller_cache_limit": "none"})
        self.assertIn("cache_caller_overrequest", codes(zero_access))

    def test_ordinary_pr_default_comes_from_context_not_event_name(self):
        result = inspect(workflow({"run": "test"}))
        self.assertEqual(result["jobs"][0]["cache"]["effective"], "write")
        unknown = inspect(workflow({"run": "test"}), context={})
        self.assertEqual(unknown["jobs"][0]["cache"]["effective"], "unknown")

    def test_job_override_and_capability_sets(self):
        for mode, restore, save in (("read", True, False), ("write", True, True), ("write-only", False, True), ("none", False, False)):
            with self.subTest(mode=mode):
                value = workflow({"run": "test"}, **{"cache-mode": "read"})
                value["jobs"]["test"]["cache-mode"] = mode
                record = inspect(value)["jobs"][0]["cache"]
                self.assertEqual((record["effective"], record["restore"], record["save"]), (mode, restore, save))

    def test_explicit_caller_limit_overrequest_is_error_not_intersection(self):
        for limit, requested in (("read", "write"), ("write-only", "read"), ("read", "write-only"), ("none", "read")):
            with self.subTest(limit=limit, requested=requested):
                result = inspect(workflow({"run": "test"}, **{"cache-mode": requested}), context={**CONTEXT, "caller_cache_limit": limit})
                self.assertIn("cache_caller_overrequest", codes(result))
                self.assertEqual(result["jobs"][0]["cache"]["effective"], "unknown")
                self.assertFalse(result["jobs"][0]["cache"]["within_caller_limit"])

    def test_omitted_callee_mode_inherits_explicit_caller_capabilities(self):
        result = inspect(workflow({"run": "test"}), context={**CONTEXT, "caller_cache_limit": "write-only"})
        self.assertEqual(result["jobs"][0]["cache"]["effective"], "write-only")
        self.assertNotIn("cache_caller_overrequest", codes(result))

    def test_implicit_low_trust_default_is_not_a_reusable_caller_limit(self):
        context = {"trigger_trust": "low-trust", "features": FEATURES}
        result = inspect(workflow({"run": "test"}, **{"cache-mode": "write"}), context=context)
        self.assertEqual(result["jobs"][0]["cache"]["effective"], "write")
        self.assertIn("low_trust_cache_write", codes(result))
        self.assertNotIn("cache_caller_overrequest", codes(result))

    def test_unresolved_limit_or_mode_does_not_claim_permissions(self):
        result = inspect(workflow({"run": "test"}, **{"cache-mode": "${{ inputs.mode }}"}))
        self.assertEqual(result["jobs"][0]["cache"]["save"], "unknown")
        result = inspect(workflow({"run": "test"}, **{"cache-mode": "read"}), context={**CONTEXT, "caller_cache_limit": "unknown"})
        self.assertEqual(result["jobs"][0]["cache"]["restore"], "unknown")
        self.assertIn("cache_access_unknown", codes(result))


class NativeStepsTest(unittest.TestCase):
    def test_unverified_parallel_member_shapes_are_unknown_not_background_proof(self):
        result = inspect(workflow({"parallel": [{"wait": "missing"}, {"parallel": [{"run": "test"}]}]}))
        self.assertIn("parallel_member_unverified", codes(result))
        self.assertTrue(all(step["background"] == "unknown" for step in result["steps"][1:]))
        self.assertEqual(result["steps"][2]["implicit_wait"], "unknown")
        self.assertFalse(result["has_errors"])

    def test_parallel_inventory_recurses_without_echoing_commands_or_env(self):
        value = workflow({"parallel": [{"run": "echo SECRET_PAYLOAD", "env": {"TOKEN": "SECRET_PAYLOAD"}},
                                       {"parallel": [{"uses": "owner/action@" + SHA}]}]},
                         {"id": "server", "run": "server", "background": True},
                         {"wait": "server"}, {"cancel": "server"}, {"wait-all": None})
        result = inspect(value)
        self.assertEqual([s["kind"] for s in result["steps"]], ["parallel", "run", "parallel", "uses", "run", "wait", "cancel", "wait-all"])
        self.assertTrue(result["steps"][1]["background"])
        self.assertTrue(result["steps"][0]["implicit_wait"])
        self.assertEqual(len(result["action_references"]), 1)
        self.assertNotIn("SECRET_PAYLOAD", json.dumps(result))
        self.assertFalse(result["has_errors"])

    def test_native_sync_rejects_if_and_invalid_arguments(self):
        for step in ({"wait": "server", "if": False}, {"cancel": "server", "if": True},
                     {"wait-all": None, "if": True}, {"wait-all": True}, {"cancel": ["a", "b"]}):
            with self.subTest(step=step):
                self.assertTrue(inspect(workflow(step))["has_errors"])

    def test_matrix_conditions_and_reusable_expansion_stay_unknown(self):
        value = workflow({"if": "success()", "run": "test"})
        value["jobs"]["test"]["strategy"] = {"matrix": {"os": ["ubuntu", "windows"]}}
        value["jobs"]["test"]["if"] = "${{ inputs.run }}"
        result = inspect(value)
        self.assertTrue({"matrix_not_expanded", "job_condition", "step_condition"} <= codes(result))
        self.assertFalse(result["complete_expansion"])


class ConcurrencyAndInputTest(unittest.TestCase):
    def test_max_queue_cannot_cancel_in_progress_but_expression_is_unknown(self):
        value = workflow({"run": "test"}, concurrency={"group": "main", "queue": "max", "cancel-in-progress": True})
        result = inspect(value)
        self.assertIn("queue_max_cancellation", codes(result))
        value["concurrency"]["cancel-in-progress"] = "${{ inputs.cancel }}"
        result = inspect(value)
        self.assertNotIn("queue_max_cancellation", codes(result))
        self.assertIn("concurrency_unresolved", codes(result))
        self.assertEqual(result["concurrency"][0]["cancel_in_progress"], "unknown")

    def test_job_concurrency_is_checked_separately(self):
        value = workflow({"run": "test"})
        value["jobs"]["test"]["concurrency"] = {"group": "main", "queue": "max", "cancel-in-progress": True}
        self.assertIn("queue_max_cancellation", codes(inspect(value)))

    def test_yaml11_on_corruption_is_rejected(self):
        for key in (True, "true"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "YAML 1.2"):
                inspector.inspect_workflow({key: "push", "jobs": {"test": {"steps": [{"run": "test"}]}}}, PATH)

    def test_context_contradictions_and_malformed_capabilities_are_rejected(self):
        for context in ({"trigger_cache_default": []}, {"trigger_trust": "low-trust", "trigger_cache_default": "write"},
                        {"features": {"cache_mode": "yes"}}, {"caller_cache_limit": []}):
            with self.subTest(context=context), self.assertRaises(ValueError):
                inspect(workflow({"run": "test"}), context=context)

    def test_cli_help_duplicate_keys_and_exit_codes(self):
        command = [sys.executable, str(ROOT / "scripts/inspect_workflows.py")]
        self.assertEqual(subprocess.run([*command, "--help"], capture_output=True).returncode, 0)
        with tempfile.TemporaryDirectory() as td:
            path, out = Path(td) / "workflow.json", Path(td) / "out.json"
            path.write_text('{"on":"push","on":"pull_request","jobs":{}}')
            with redirect_stderr(io.StringIO()):
                self.assertEqual(inspector.main(["--workflow", str(path), "--path", PATH, "--out", str(out)]), 2)
            self.assertFalse(out.exists())
            path.write_text(json.dumps(workflow({"run": "test"}, concurrency={"group": "x", "queue": "max", "cancel-in-progress": True})))
            self.assertEqual(inspector.main(["--workflow", str(path), "--path", PATH, "--out", str(out)]), 1)
            self.assertTrue(json.loads(out.read_text())["advisory_only"])


if __name__ == "__main__":
    unittest.main()
