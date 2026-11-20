import json
import unittest

from scripts.evaluate_shadow import SCHEMA, evaluate, unique_keys


def pair():
    provenance = {"revision": "a" * 40, "context_digest": "sha256:" + "b" * 64,
                  "complete": True, "state": "completed"}
    return {"schema": SCHEMA, "inventory_complete": True, "inventory": ["a", "b"],
            "selection": ["a"],
            "full": dict(provenance, outcomes={"a": "passed", "b": "passed"}),
            "selected": dict(provenance, outcomes={"a": "passed"})}


class ShadowTests(unittest.TestCase):
    def test_clean_pair_never_authorizes_selection(self):
        result = evaluate(pair())
        self.assertEqual(result["status"], "observed_clean")
        self.assertEqual(result["recommendation"], "FULL")
        self.assertEqual(result["selection_fraction"], 0.5)

    def test_missed_failure(self):
        data = pair()
        data["full"]["outcomes"]["b"] = "failed"
        result = evaluate(data)
        self.assertEqual(result["status"], "missed_failure")
        self.assertEqual(result["missed_failures"], ["b"])

    def test_disagreement_and_matched_failure(self):
        data = pair()
        data["full"]["outcomes"]["a"] = "failed"
        self.assertEqual(evaluate(data)["status"], "outcome_disagreement")
        data["selected"]["outcomes"]["a"] = "failed"
        self.assertEqual(evaluate(data)["status"], "matched_failure")

    def test_incomplete_or_incomparable_cannot_pass(self):
        for change in ({"revision": "c" * 40}, {"context_digest": "sha256:" + "c" * 64},
                       {"complete": False}, {"state": "cancelled"}, {"outcomes": {}},
                       {"outcomes": {"a": "skipped"}}, {"outcomes": {"a": "error"}},
                       {"outcomes": {"a": []}}, {"outcomes": {"a": "passed", "b": "passed"}}):
            with self.subTest(change=change):
                data = pair()
                data["selected"].update(change)
                self.assertEqual(evaluate(data)["status"], "unknown")

    def test_incomplete_inventory_or_duplicate_ids(self):
        for change in ({"inventory_complete": False}, {"inventory": ["a", "a"]},
                       {"selection": []}, {"selection": ["c"]}):
            data = pair()
            data.update(change)
            self.assertEqual(evaluate(data)["status"], "unknown")

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaises(ValueError):
            json.loads('{"t":"failed","t":"passed"}', object_pairs_hook=unique_keys)
