from __future__ import annotations

import contextlib
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import collect_gh_cache as cache
from scripts.collect_gh import GhClient, GhError


def item(id_):
    return {"id": id_, "key": "raw-key", "version": "raw-version", "ref": "refs/heads/main",
            "size_in_bytes": 7, "created_at": "2026-11-19T01:01:01Z",
            "last_accessed_at": "2026-11-19T01:01:01Z"}


def response(status):
    body = {"total_count": 0, "actions_caches": []} if status == 200 else {}
    return SimpleNamespace(returncode=0 if status == 200 else 1,
                           stdout=f"HTTP/2.0 {status} Response\n\n" + json.dumps(body), stderr="")


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

    def test_strict_cli_default_preserves_first_503_and_does_not_retry(self):
        replies, calls, waits, clients = [response(503), response(200)], [], [], []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return replies.pop(0)

        def client(**kwargs):
            value = GhClient(**kwargs, runner=runner, sleeper=waits.append)
            clients.append(value)
            return value

        output = io.StringIO()
        with patch.object(cache, "GhClient", side_effect=client), contextlib.redirect_stdout(output):
            result = cache.main(["--repo", "x/y"])
        report = json.loads(output.getvalue())
        self.assertEqual(result, 3)
        self.assertFalse(report["complete"])
        self.assertEqual(report["gaps"], ["http_error"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(replies), 1)
        self.assertEqual(waits, [])
        self.assertEqual(report["transport"]["stats"], {"requests": 1, "retries": 0,
                                                        "conditional_hits": 0, "cache_write_errors": 0})
        self.assertEqual(report["transport"]["policy"], {"timeout_s": 10, "retries_per_request": 0,
                                                         "max_wait_s_per_request": 0, "max_pages": 5})
        self.assertTrue(report["transport"]["counters_complete"])
        self.assertEqual(clients[0].retries, 0)
        self.assertEqual(calls[0][1]["timeout"], 10)
        self.assertNotIn("shell", calls[0][1])

    def test_opt_in_metadata_retry_is_bounded_and_visible(self):
        replies, calls, waits = [response(503), response(200)], [], []

        def runner(*args, **kwargs):
            calls.append(args)
            return replies.pop(0)

        output = io.StringIO()
        with patch.object(cache, "GhClient", side_effect=lambda **kwargs: GhClient(
                **kwargs, runner=runner, sleeper=waits.append)), contextlib.redirect_stdout(output):
            result = cache.main(["--repo", "x/y", "--retries", "1", "--max-pages", "1"])
        report = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertTrue(report["complete"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(waits, [1])
        self.assertEqual(report["transport"]["observed_api_calls"], 1)
        self.assertEqual(report["transport"]["stats"]["requests"], 2)
        self.assertEqual(report["transport"]["stats"]["retries"], 1)
        self.assertEqual(report["transport"]["policy"]["retries_per_request"], 1)
        self.assertEqual(report["transport"]["policy"]["max_wait_s_per_request"], 2)

    def test_transport_statistics_cover_only_this_collection(self):
        replies = iter([response(503), response(200), response(200)])
        client = GhClient(retries=1, max_wait=2, runner=lambda *a, **k: next(replies), sleeper=lambda _: None)
        first, second = cache.collect(client, "x/y", 1), cache.collect(client, "x/y", 1)
        self.assertEqual(first["transport"]["stats"]["requests"], 2)
        self.assertEqual(first["transport"]["stats"]["retries"], 1)
        self.assertEqual(second["transport"]["stats"]["requests"], 1)
        self.assertEqual(second["transport"]["stats"]["retries"], 0)
        self.assertEqual(client.stats["requests"], 3)

    def test_exhausted_transport_preserves_failure_and_counters(self):
        calls, waits = [], []

        def runner(*args, **kwargs):
            calls.append(args)
            return response(503)

        client = GhClient(retries=5, max_wait=2, runner=runner, sleeper=waits.append)
        report = cache.collect(client, "x/y", 1)
        self.assertFalse(report["complete"])
        self.assertEqual(report["gaps"], ["http_error"])
        self.assertIsNone(report["complete_inventory_bytes"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(waits, [1])
        self.assertEqual(report["transport"]["stats"]["requests"], 2)
        self.assertEqual(report["transport"]["stats"]["retries"], 1)
        self.assertEqual(report["transport"]["policy"]["retries_per_request"], 5)

    def test_uninstrumented_callables_do_not_invent_zero_transport_counts(self):
        report = cache.collect(lambda _: {"total_count": 0, "actions_caches": []}, "x/y", 1)
        self.assertTrue(report["complete"])
        self.assertFalse(report["transport"]["counters_complete"])
        self.assertTrue(all(v is None for v in report["transport"]["stats"].values()))
        self.assertIsNone(report["transport"]["policy"]["retries_per_request"])
        self.assertEqual(report["transport"]["observed_api_calls"], 1)

    def test_transport_projection_rejects_invalid_counts_and_never_exposes_extra_data(self):
        for bad in (True, -1, 10 ** 30, float('nan'), "private-token"):
            def get(_):
                get.stats["requests"] = bad
                return {"total_count": 0, "actions_caches": []}
            get.stats = {key: 0 for key in cache.COUNTERS}
            get.stats["authorization"] = "private-token"
            get.timeout, get.retries, get.max_wait = float('inf'), 0, -0.5
            report = cache.collect(get, "x/y", 1)
            self.assertIsNone(report["transport"]["stats"]["requests"])
            self.assertFalse(report["transport"]["counters_complete"])
            self.assertIsNone(report["transport"]["policy"]["timeout_s"])
            self.assertIsNone(report["transport"]["policy"]["max_wait_s_per_request"])
            self.assertNotIn("private-token", json.dumps(report, allow_nan=False))

    def test_invalid_retry_and_page_budgets_make_no_provider_call(self):
        with patch.object(cache, "GhClient") as client, contextlib.redirect_stderr(io.StringIO()):
            for retries in ("-1", "6", "nan"):
                with self.assertRaises(SystemExit) as raised:
                    cache.main(["--repo", "x/y", "--retries", retries])
                self.assertEqual(raised.exception.code, 2)
            client.assert_not_called()
        for repo, pages in (("x/y", 0), ("x/y", 21), ("x/y", True), ("x/..", 1), ("x/y/z", 1)):
            with self.assertRaises(ValueError): cache.collect(lambda _: self.fail("unexpected API call"), repo, pages)
