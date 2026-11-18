"""Adapter routing requires positive stack evidence and never certifies support."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import discover_repo


class AdapterDiscoveryTests(unittest.TestCase):
    def discover(self, files):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            return discover_repo.discover(root)

    def test_polyglot_routes_only_evidenced_adapters(self):
        report = self.discover({"go.mod": "module example.invalid/app", "web/package.json": json.dumps({
            "devDependencies": {"vitest": "5.0.0", "@playwright/test": "1.0.0"},
            "dependencies": {"next": "16"}}), "Dockerfile": "FROM scratch"})
        adapters = {x["adapter"]: x for x in report["adapter_candidates"]}
        self.assertEqual(set(adapters), {"go", "nextjs", "vitest", "playwright", "docker"})
        for adapter in adapters.values():
            self.assertEqual(adapter["version_support"], "unknown")
            self.assertEqual(adapter["measured_benefit"], "unknown")

    def test_non_js_repository_does_not_get_js_adapters(self):
        report = self.discover({"pyproject.toml": "[project]", "src/app.py": "pass"})
        self.assertEqual(report["adapter_candidates"], [])
        self.assertEqual(report["validation_topology"]["required_questions"]["release"]["status"], "unknown")

    def test_malformed_package_and_filename_only_evidence(self):
        report = self.discover({"package.json": "[]", "web/next.config.ts": "SECRET_DO_NOT_READ",
                                "tests/example_test.go": "SECRET_DO_NOT_READ"})
        self.assertEqual({x["adapter"] for x in report["adapter_candidates"]}, {"nextjs", "go"})
        self.assertNotIn("SECRET_DO_NOT_READ", json.dumps(report))

    def test_dependency_graph_marker_does_not_claim_remote_cache_is_safe(self):
        report = self.discover({"nx.json": "{}"})
        adapter = report["adapter_candidates"][0]
        self.assertEqual(adapter["adapter"], "remote-task-cache")
        self.assertEqual(adapter["status"], "candidate_requires_review")


if __name__ == "__main__":
    unittest.main()
