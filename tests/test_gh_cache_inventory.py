from __future__ import annotations

import unittest

from scripts import collect_gh_cache as cache
from scripts.collect_gh import GhError


def item(id_):
    return {"id": id_, "key": "raw-key", "version": "raw-version", "ref": "refs/heads/main",
            "size_in_bytes": 7, "created_at": "2026-11-19T01:01:01Z",
            "last_accessed_at": "2026-11-19T01:01:01Z"}


class CacheInventoryTests(unittest.TestCase):
    def test_complete_inventory_does_not_expose_raw_key(self):
        calls = []

        def get(path):
            calls.append(path)
            return {"total_count": 1, "actions_caches": [item(1)]}

        report = cache.collect(get, "x/y", 1)
        self.assertTrue(report["complete"])
        self.assertEqual(report["complete_inventory_bytes"], 7)
        self.assertEqual(report["caches"][0]["id"], 1)
        self.assertNotIn("raw-key", str(report))
        self.assertTrue(calls[0].startswith("repos/x/y/actions/caches?"))

    def test_budget_does_not_count_partial_bytes_as_complete(self):
        get = lambda _: {"total_count": 101, "actions_caches": [item(i) for i in range(1, 101)]}
        report = cache.collect(get, "x/y", 1)
        self.assertFalse(report["complete"])
        self.assertIsNone(report["complete_inventory_bytes"])
        self.assertIn("page_budget_reached", report["gaps"])

    def test_duplicate_identity_and_churn_never_complete(self):
        first = {"total_count": 101, "actions_caches": [item(i) for i in range(1, 101)]}
        for count, reason in ((101, "duplicate_cache_identity"), (102, "inventory_changed_during_collection")):
            with self.subTest(count=count):
                pages = iter([first, {"total_count": count, "actions_caches": [item(1)]}])
                report = cache.collect(lambda _: next(pages), "x/y", 2)
                self.assertFalse(report["complete"])
                self.assertIn(reason, report["gaps"])

    def test_access_error_is_not_an_empty_inventory(self):
        def get(_):
            raise GhError(kind="http_error", status_code=404)
        report = cache.collect(get, "x/y", 1)
        self.assertFalse(report["complete"])
        self.assertIsNone(report["provider_reported_count"])
