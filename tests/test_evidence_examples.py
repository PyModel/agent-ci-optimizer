"""Run the shipped examples through their consumers to catch documentation drift."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import analyze_gh
import benchmark
import evaluate_validation
import graph_metrics


def example(name):
    return json.loads((ROOT / "templates" / name).read_text())


class EvidenceExampleTests(unittest.TestCase):
    def test_synthetic_policy_keeps_low_sample_visible(self):
        policy = example("validation-policy.example.json")
        evidence = example("validation-evidence.example.json")
        now = evaluate_validation.stamp("2026-11-17T12:10:00Z")
        result = evaluate_validation.evaluate(policy, evidence, now)
        self.assertEqual(result["pr_budget"]["state"], "insufficient")
        self.assertEqual(result["release"]["state"], "pass")
        self.assertEqual(result["rollout"]["state"], "eligible_to_promote")

    def test_graph_example_recomputes_upstream_savings(self):
        result = graph_metrics.evaluate_graph(example("job-graph.example.json"))
        self.assertEqual(result["required_completion_s"], 305)
        self.assertEqual(result["counterfactuals"][0]["required_completion_s"], 205)

    def test_benchmark_example_requires_user_evidence(self):
        context = benchmark.read_metadata(ROOT / "templates" / "benchmark-context.example.json")
        self.assertTrue(all(value == "unknown" for value in context.values()))

    def test_cohort_example_preserves_run_identity(self):
        result = analyze_gh.apply_cohort_context([{"id": 123}], example("cohort-context.example.json"))
        self.assertEqual(result[0]["id"], 123)
        self.assertEqual(result[0]["cohort"]["trust"], "trusted-push")


if __name__ == "__main__":
    unittest.main()
