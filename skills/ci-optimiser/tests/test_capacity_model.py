"""Capacity and cost are explicit conditional models, never billing evidence."""
import copy
import json
import subprocess
import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from capacity_model import evaluate_capacity
from graph_metrics import evaluate_graph


def fixture(capacity=2):
    return {"schema": "ci-capacity-model/1", "graph": {
        "schema": "ci-job-graph/1", "complete": True, "matrix_resolved": True,
        "required_nodes": ["a", "b"], "nodes": [
            {"id": key, "needs": [], "duration_s": 60} for key in ("a", "b")]},
        "pools": {"linux": {"capacity": capacity, "pricing": {
            "per_minute": 0.1, "billing_quantum_s": 60, "currency": "USD", "source": "fixture, not a provider price"}}},
        "node_resources": {key: {"pool": "linux", "exclusive": []} for key in ("a", "b")}}


class CapacityTest(unittest.TestCase):
    def test_finite_capacity_changes_elapsed_not_runner_cost(self):
        serial, parallel = evaluate_capacity(fixture(1)), evaluate_capacity(fixture(2))
        self.assertEqual((serial["elapsed_s"], parallel["elapsed_s"]), (120, 60))
        for result in (serial, parallel):
            self.assertEqual(result["runner_s"], 120)
            self.assertAlmostEqual(result["modeled_cost"]["amount"], .2)
            self.assertEqual(result["observed_cost"]["amount"], "unknown")

    def test_exclusive_resource_is_global_across_pools(self):
        spec = fixture()
        spec["pools"]["mac"] = {"capacity": 2}
        spec["node_resources"]["b"]["pool"] = "mac"
        for binding in spec["node_resources"].values():
            binding["exclusive"] = ["device"]
        result = evaluate_capacity(spec)
        self.assertEqual(result["elapsed_s"], 120)
        self.assertEqual(result["modeled_cost"]["status"], "unknown")

    def test_dependency_and_zero_duration_release(self):
        spec = fixture(1)
        spec["graph"]["nodes"][0]["duration_s"] = 0
        spec["graph"]["nodes"][1]["needs"] = ["a"]
        result = evaluate_capacity(spec)
        self.assertEqual(result["elapsed_s"], 60)
        self.assertEqual(result["schedule"][1]["start_s"], 0)

    def test_required_gate_and_all_job_elapsed_are_distinct(self):
        spec = fixture()
        spec["graph"]["required_nodes"] = ["a"]
        spec["graph"]["nodes"][1]["duration_s"] = 180
        result = evaluate_capacity(spec)
        self.assertEqual(result["required_completion_s"], 60)
        self.assertEqual(result["elapsed_s"], 180)
        self.assertEqual(result["runner_s"], 240)

    def test_per_job_rounding_not_elapsed_rounding(self):
        spec = fixture()
        for node in spec["graph"]["nodes"]:
            node["duration_s"] = 1
        result = evaluate_capacity(spec)
        self.assertEqual(result["elapsed_s"], 1)
        self.assertEqual(result["runner_s"], 2)
        self.assertAlmostEqual(result["modeled_cost"]["amount"], .2)

    def test_explicit_free_rate_vs_missing_rate(self):
        spec = fixture()
        spec["pools"]["linux"]["pricing"]["per_minute"] = 0
        self.assertEqual(evaluate_capacity(spec)["modeled_cost"]["amount"], 0)
        del spec["pools"]["linux"]["pricing"]["per_minute"]
        self.assertEqual(evaluate_capacity(spec)["modeled_cost"]["amount"], "unknown")

    def test_observed_is_separate_and_requires_provenance(self):
        spec = fixture()
        spec["observed_cost"] = {"amount": 3, "currency": "USD", "source": "invoice 42", "scope": "full month"}
        result = evaluate_capacity(spec)
        self.assertEqual(result["observed_cost"]["status"], "observed")
        self.assertEqual(result["observed_cost"]["amount"], 3)
        self.assertEqual(result["modeled_cost"]["status"], "modeled")
        del spec["observed_cost"]["scope"]
        self.assertEqual(evaluate_capacity(spec)["observed_cost"]["status"], "unknown")

    def test_invalid_capacity_and_bindings_fail_closed(self):
        for value in (0, -1, True, 1.5, "unknown"):
            self.assertEqual(evaluate_capacity(fixture(value))["status"], "unknown")
        for edit in (lambda s: s["node_resources"].pop("a"),
                     lambda s: s["node_resources"]["a"].update(pool=[]),
                     lambda s: s["node_resources"]["a"].update(exclusive=["lock", "lock"]),
                     lambda s: s["graph"]["nodes"][0].update(needs=["b"])
                     or s["graph"]["nodes"][1].update(needs=["a"])):
            spec = fixture()
            edit(spec)
            self.assertEqual(evaluate_capacity(spec)["status"], "unknown")

    def test_mixed_currencies_do_not_sum(self):
        spec = fixture()
        spec["pools"]["other"] = copy.deepcopy(spec["pools"]["linux"])
        spec["pools"]["other"]["pricing"]["currency"] = "EUR"
        spec["node_resources"]["b"]["pool"] = "other"
        self.assertEqual(evaluate_capacity(spec)["modeled_cost"]["amount"], "unknown")

    def test_nonfinite_and_extreme_numeric_values_are_unknown(self):
        for value in (float("nan"), float("inf"), 10 ** 1000):
            spec = fixture()
            spec["graph"]["nodes"][0]["duration_s"] = value
            self.assertEqual(evaluate_capacity(spec)["status"], "unknown")

    def test_cli_json_and_duplicate_keys(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "capacity_model.py"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capacity.json"
            path.write_text(json.dumps(fixture()), encoding="utf-8")
            result = subprocess.run([sys.executable, str(script), str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "modeled")
            path.write_text('{"schema":"a", "schema":"b"}', encoding="utf-8")
            result = subprocess.run([sys.executable, str(script), str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("duplicate JSON key", result.stderr)

    def test_existing_graph_and_input_are_unchanged(self):
        spec = fixture(1)
        before = copy.deepcopy(spec)
        evaluate_capacity(spec)
        self.assertEqual(spec, before)
        self.assertEqual(evaluate_graph(spec["graph"])["required_completion_s"], 60)


if __name__ == "__main__":
    unittest.main()
