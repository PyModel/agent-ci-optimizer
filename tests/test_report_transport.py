"""Missing or invalid shard reports must not disappear during a merge."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_reports


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.reports = self.root / "reports"
        for shard in (1, 2):
            folder = self.reports / f"blob-{shard}"
            folder.mkdir(parents=True)
            (folder / "blob-same-name.json").write_text(json.dumps({"shard": shard}))

    def test_staging_preserves_same_named_reports(self):
        target = self.root / "staged"
        with contextlib.redirect_stdout(io.StringIO()):
            status = check_reports.main(["--root", str(self.reports), "--expected-shards", "1", "2",
                                         "--stage-out", str(target)])
        self.assertEqual(status, 0)
        self.assertEqual(len(list(target.iterdir())), 2)
        self.assertEqual({json.loads(path.read_text())["shard"] for path in target.iterdir()}, {1, 2})

    def test_missing_and_extra_shards_fail(self):
        self.assertTrue(check_reports.inspect(self.reports, ["1", "2", "3"])[0])
        self.assertTrue(check_reports.inspect(self.reports, ["1"])[0])

    def test_bad_empty_and_symlink_reports_fail(self):
        path = self.reports / "blob-1" / "blob-same-name.json"
        for content in ("", "not json", "[]", "null"):
            path.write_text(content)
            self.assertTrue(check_reports.inspect(self.reports, ["1", "2"])[0])
        path.unlink()
        path.symlink_to(self.reports / "blob-2" / "blob-same-name.json")
        self.assertTrue(check_reports.inspect(self.reports, ["1", "2"])[0])

    def test_failure_never_publishes_staging_directory(self):
        target = self.root / "staged"
        with contextlib.redirect_stdout(io.StringIO()):
            status = check_reports.main(["--root", str(self.reports), "--expected-shards", "1", "2", "3",
                                         "--stage-out", str(target)])
        self.assertEqual(status, 1)
        self.assertFalse(target.exists())

    def test_existing_reports_are_never_overwritten(self):
        target = self.root / "staged"
        target.mkdir()
        (target / "keep").write_text("evidence")
        with contextlib.redirect_stderr(io.StringIO()):
            status = check_reports.main(["--root", str(self.reports), "--expected-shards", "1", "2",
                                         "--stage-out", str(target)])
        self.assertEqual(status, 2)
        self.assertEqual((target / "keep").read_text(), "evidence")

    def test_duplicate_shard_id_and_size_limit_fail(self):
        self.assertTrue(check_reports.inspect(self.reports, ["1", "1"])[0])
        self.assertTrue(check_reports.inspect(self.reports, ["1", "2"], max_bytes=1)[0])

    def test_adversarial_shard_and_report_names_cannot_collide(self):
        root = self.root / "collision"
        first = root / "blob-1"
        second = root / "blob-1--1--blob-2"
        first.mkdir(parents=True)
        second.mkdir()
        (first / "blob-2--1--blob-x.json").write_text('{"value":1}')
        (second / "blob-x.json").write_text('{"value":2}')
        errors, payloads = check_reports.inspect(root, ["1", "1--1--blob-2"])
        self.assertEqual(errors, [])
        self.assertEqual(len(payloads), 2)
        self.assertEqual({json.loads(raw)["value"] for raw in payloads.values()}, {1, 2})

    def test_unreadable_subtree_cannot_silently_drop_reports(self):
        nested = self.reports / "blob-1" / "unreadable"
        nested.mkdir()
        (nested / "blob-hidden.json").write_text('{"value":1}')
        real = os.scandir

        def guarded(path):
            if Path(path) == nested:
                raise PermissionError("fixture unreadable")
            return real(path)

        with mock.patch("os.scandir", side_effect=guarded):
            errors, _ = check_reports.inspect(self.reports, ["1", "2"])
        self.assertTrue(any("unreadable directory" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
