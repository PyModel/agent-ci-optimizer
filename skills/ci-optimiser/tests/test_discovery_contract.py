"""Validation evidence is advisory; discovery stays bounded and read-only."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import discover_repo as discovery  # noqa: E402


def write(root: Path, name: str, text: str = "") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class ValidationEvidenceTest(unittest.TestCase):
    def assert_unconfirmed(self, topology):
        self.assertEqual(topology["schema"], "validation-topology/1")
        self.assertEqual(topology["status"], "needs_review")
        self.assertEqual(topology["category_assignments"], "unknown")
        self.assertEqual(set(topology["required_questions"]), {"rules", "change", "integration", "release"})
        self.assertTrue(all(q["status"] == "unknown" for q in topology["required_questions"].values()))
        self.assertTrue(topology["unknowns"])

    def test_js_graph_and_delivery_sources_are_evidence_not_certification(self):
        secret = "SYNTHETIC_COMMAND_SECRET_8192"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "package.json", json.dumps({
                "scripts": {"lint": "eslint .", "test:integration": f"TOKEN={secret} test-runner", "release": "publish"},
                "devDependencies": {"nx": "1", "vitest": "1", "next": "1", "oxfmt": "1"},
            }))
            for path in ("nx.json", "pnpm-lock.yaml", "vitest.config.ts", ".husky/pre-commit",
                         "tests/integration/order.test.ts", "Makefile", "ci-map.json",
                         ".github/actions.lock", "release.config.js", "vercel.json", "docs/rollback.md"):
                write(root, path, secret)
            write(root, ".github/workflows/ci.yml", """on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
  merge_group:
cache-mode: read
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - background: true
        run: echo SYNTHETIC_COMMAND_SECRET_8192
      - parallel:
          - run: echo SYNTHETIC_COMMAND_SECRET_8192
""")
            result = discovery.discover(root)
        topology = result["validation_topology"]
        self.assert_unconfirmed(topology)
        evidence = topology["evidence_files"]
        self.assertIn("nx.json", evidence["dependency_graph_inputs"])
        self.assertIn(".husky/pre-commit", evidence["hooks_and_rules"])
        self.assertIn("tests/integration/order.test.ts", evidence["integration_candidates"])
        self.assertIn("Makefile", evidence["full_suite_entrypoint_candidates"])
        self.assertIn("ci-map.json", evidence["validation_policy_candidates"])
        self.assertIn("release.config.js", evidence["release_candidates"])
        self.assertIn("vercel.json", evidence["deployment_candidates"])
        self.assertIn("docs/rollback.md", evidence["rollout_policy_candidates"])
        self.assertEqual(evidence["action_lockfiles"], [".github/actions.lock"])
        self.assertEqual(topology["package_script_names"][0]["names"], ["lint", "release", "test:integration"])
        self.assertTrue({"next", "oxfmt"} <= set(result["package_json"]["package.json"]["tooling_deps"]))
        markers = {m["marker"] for m in topology["workflow_markers"]}
        self.assertTrue({"merge_group", "ready_for_review", "background", "parallel", "cache_mode"} <= markers)
        self.assertNotIn(secret, json.dumps(result))
        self.assertFalse(topology["providers"][0]["expanded"])

    def test_go_python_repository_does_not_require_javascript(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for path in ("go.mod", "go.sum", "pyproject.toml", "pytest.ini", "Makefile", ".golangci.yml",
                         "internal/cart/cart_test.go", "tests/test_model.py", "tests/integration/test_api.py"):
                write(root, path)
            result = discovery.discover(root)
        self.assertEqual(result["languages"], ["go", "python"])
        self.assertFalse(result["hints"]["js_modules_applicable"])
        topology = result["validation_topology"]
        self.assert_unconfirmed(topology)
        self.assertEqual(topology["package_script_names"], [])
        self.assertIn("go.mod", topology["evidence_files"]["dependency_graph_inputs"])
        self.assertIn("tests/integration/test_api.py", topology["evidence_files"]["integration_candidates"])
        self.assertIn("internal/cart/cart_test.go", topology["evidence_files"]["test_configuration_and_sources"])

    def test_manifestless_languages_keep_source_evidence_without_inventing_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for path in ("scripts/audit.py", "backend/server.go", "ui/widget.tsx", "native/engine.rs"):
                write(root, path)
            result = discovery.discover(root)
        self.assertEqual(result["languages"], ["go", "javascript/typescript", "python", "rust"])
        self.assertEqual(result["language_evidence"]["python"]["source_examples"], ["scripts/audit.py"])
        self.assertEqual(result["language_evidence"]["go"]["source_examples"], ["backend/server.go"])
        self.assertEqual(result["language_evidence"]["javascript/typescript"]["source_examples"], ["ui/widget.tsx"])
        self.assertTrue(all(not e["manifest_examples"] for e in result["language_evidence"].values()))
        self.assertEqual(result["package_json"], {})
        self.assertEqual(result["validation_topology"]["package_script_names"], [])
        self.assert_unconfirmed(result["validation_topology"])

    def test_other_providers_remain_unparsed_and_external_gates_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, ".gitlab-ci.yml", "include: secret/external-config.yml\n")
            write(root, ".circleci/config.yml", "workflows: {release: {jobs: [ship]}}\n")
            with mock.patch.object(discovery, "read_text", wraps=discovery.read_text) as reader:
                result = discovery.discover(root)
            reader.assert_not_called()
        topology = result["validation_topology"]
        self.assert_unconfirmed(topology)
        self.assertEqual({p["provider"] for p in topology["providers"]}, {"circleci", "gitlab_ci"})
        self.assertTrue(all(p["analysis"] == "filenames_only" for p in topology["providers"]))
        self.assertTrue(all(not p["expanded"] for p in topology["providers"]))
        self.assertEqual(topology["evidence_files"]["release_candidates"], [])
        self.assertTrue(any("external" in unknown for unknown in topology["unknowns"]))

    def test_unsupported_workflow_and_job_names_cannot_certify_categories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, ".github/workflows/ci.yml", """on: [push]
defaults: &shared {run: {shell: bash}}
jobs:
  release:
    uses: ${{ inputs.external_workflow }}
  integration:
    <<: *shared
    steps: [{run: 'true'}]
""")
            result = discovery.discover(root)
        topology = result["validation_topology"]
        self.assert_unconfirmed(topology)
        self.assertEqual(topology["evidence_files"]["release_candidates"], [])
        self.assertEqual(topology["evidence_files"]["integration_candidates"], [])
        self.assertEqual(topology["providers"][0]["analysis"], "line_markers_only")
        self.assertIn("unresolved_action_reference", result["github_workflows"][".github/workflows/ci.yml"])
        self.assertEqual(result["hints"]["pinned_action_ratio"], "unknown")

    def test_quoted_actions_counted_but_lockfile_does_not_prove_resolution(self):
        sha = "a" * 40
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, ".github/actions.lock", "{}")
            write(root, ".github/workflows/ci.yml", f"""on: push
jobs:
  t:
    steps:
      - uses: 'actions/checkout@{sha}' # fixed ref
      - uses: "actions/setup-node@v7"
      - uses: ${{{{ inputs.action }}}}
""")
            result = discovery.discover(root)
        metadata = result["github_workflows"][".github/workflows/ci.yml"]
        self.assertEqual(metadata["action_pins"], {"pinned": 1, "total": 2})
        self.assertEqual(metadata["checkout"][0]["value"], sha)
        self.assertEqual(metadata["unpinned_action"][0]["value"], "actions/setup-node@v7")
        self.assertEqual(result["hints"]["pinned_action_ratio"]["value"], 0.5)
        self.assert_unconfirmed(result["validation_topology"])
        self.assertTrue(any("not resolved" in u for u in result["validation_topology"]["unknowns"]))

    def test_matching_commands_and_conditions_do_not_emit_source_text(self):
        secret = "SYNTHETIC_SECRET_NEVER_EMIT"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, ".github/workflows/ci.yml", f"""on: push
jobs:
  t:
    if: env.TOKEN == '{secret}'
    steps:
      - run: echo {secret} pull_request_target
      - run: rm -rf /tmp/{secret}
      - run: echo {secret} workflow_run || true
""")
            result = discovery.discover(root)
        self.assertNotIn(secret, json.dumps(result))
        metadata = result["github_workflows"][".github/workflows/ci.yml"]
        self.assertIn("pull_request_target", metadata)  # A text marker, not an event conclusion.
        self.assertEqual(metadata["no_cache_or_clean"][0]["value"], "rm -rf")

    def test_invalid_package_schema_does_not_abort_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for number, content in enumerate(("[]", "null", "42", '"value"', "{")):
                write(root, f"packages/bad{number}/package.json", content)
            write(root, "packages/valid/package.json", '{"scripts":{"test":"echo secret"}}')
            result = discovery.discover(root)
        topology = result["validation_topology"]
        self.assertEqual(len(topology["inventory"]["content_errors"]), 5)
        self.assertEqual(topology["inventory"]["coverage"], "partial")
        self.assertEqual(topology["package_script_names"], [{"path": "packages/valid/package.json", "names": ["test"]}])
        self.assert_unconfirmed(topology)

    def test_truncated_inventory_still_has_unknown_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("Makefile", "go.mod", "release.config.js"):
                write(root, name)
            with mock.patch.object(discovery, "MAX_FILES", 2):
                result = discovery.discover(root)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["validation_topology"]["inventory"]["coverage"], "partial")
        self.assert_unconfirmed(result["validation_topology"])


class SafeDiscoveryReadTest(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, "symlink"), "requires symlinks")
    def test_external_file_and_directory_symlinks_are_excluded(self):
        sentinel = "SYNTHETIC_OUTSIDE_METADATA"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "repo"
            root.mkdir()
            external = write(base, "outside/package.json", json.dumps({"packageManager": sentinel}))
            (root / "package.json").symlink_to(external)
            (root / "packages").symlink_to(external.parent, target_is_directory=True)
            workflow = root / ".github/workflows/ci.yml"
            workflow.parent.mkdir(parents=True)
            workflow.symlink_to(external)
            result = discovery.discover(root)
            self.assertIsNone(discovery.read_text(external, root))
            self.assertIsNone(discovery.read_text(root / "package.json", root))
            self.assertIsNone(discovery.read_text(root / "packages/package.json", root))
        self.assertNotIn(sentinel, json.dumps(result))
        self.assertEqual(result["package_json"], {})
        self.assertEqual(result["github_workflows"], {})
        self.assertEqual(len(result["validation_topology"]["inventory"]["excluded_or_unreadable"]), 3)
        self.assertEqual(result["validation_topology"]["inventory"]["coverage"], "partial")

    @unittest.skipUnless(hasattr(os, "symlink"), "requires symlinks")
    def test_symlink_root_rejected_and_nested_ancestor_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            original = write(base, "outside/nested/package.json", "{}")
            linked = base / "linked"
            linked.symlink_to(base / "outside", target_is_directory=True)
            with redirect_stderr(io.StringIO()):
                self.assertEqual(discovery.main(["--root", str(linked)]), 2)
            self.assertIsNone(discovery.read_text(linked / "nested/package.json"))
            self.assertEqual(discovery.read_text(original), "{}")

    def test_byte_limit_is_enforced_on_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            small = write(root, "small", "hello")
            large = write(root, "large", "x" * 101)
            with mock.patch.object(discovery, "MAX_READ_BYTES", 100):
                self.assertEqual(discovery.read_text(small, root), "hello")
                self.assertIsNone(discovery.read_text(large, root))
                # A file can grow after its metadata was observed: bound the read itself too.
                with mock.patch.object(discovery.os, "fstat", return_value=mock.Mock(st_mode=0o100600, st_size=0)):
                    self.assertIsNone(discovery.read_text(large, root))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX special files")
    def test_nonregular_file_does_not_block_or_get_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pipe = root / "package.json"
            os.mkfifo(pipe)
            self.assertIsNone(discovery.read_text(pipe, root))
            result = discovery.discover(root)
        self.assertEqual(result["package_json"], {})
        self.assertEqual(result["validation_topology"]["inventory"]["coverage"], "partial")


if __name__ == "__main__":
    unittest.main()
