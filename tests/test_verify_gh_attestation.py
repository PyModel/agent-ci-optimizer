from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import verify_gh_attestation as adapter


class AttestationTests(unittest.TestCase):
    def test_local_digest_matches_verified_subject(self):
        digest = hashlib.sha256(b"artifact").hexdigest()
        raw = json.dumps([{"verificationResult": {
            "signature": {"certificate": {"issuer": "fixture"}},
            "statement": {"predicateType": adapter.PREDICATE, "subject": [{"digest": {"sha256": digest}}]},
        }}]).encode()
        self.assertEqual(adapter.check_output(raw, digest), 1)
        for bad in (b"[]", b'{"verified":true}', raw.replace(digest.encode(), b"0" * 64)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                adapter.check_output(bad, digest)

    def test_pinned_cli_and_exact_constraints_without_release_claim(self):
        with tempfile.TemporaryDirectory() as temp:
            gh = Path(temp) / "gh"
            artifact = Path(temp) / "deliverable"
            gh.write_bytes(b"fixture")
            artifact.write_bytes(b"artifact")
            args = ["--gh", str(gh), "--gh-sha256", hashlib.sha256(b"fixture").hexdigest(),
                    "--artifact", str(artifact), "--repo", "owner/project", "--signer-workflow",
                    "owner/signer/.github/workflows/build.yml", "--source-digest", "a" * 40,
                    "--signer-digest", "b" * 40]
            with patch.object(adapter, "run", return_value=b"fixture evidence") as run, \
                 patch.object(adapter, "check_output", return_value=1), \
                 contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(adapter.main(args), 0)
                command = run.call_args.args[0]
                self.assertIn("--deny-self-hosted-runners", command)
                self.assertIn("--source-digest", command)
                self.assertIn("--signer-digest", command)
                self.assertEqual(json.loads(stdout.getvalue())["release_eligibility"], "unknown")
            gh.write_bytes(b"changed")
            with patch.object(adapter, "run") as run, contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(adapter.main(args), 3)
                run.assert_not_called()
                self.assertEqual(json.loads(stdout.getvalue())["reason"], "verifier_digest_mismatch")

    @unittest.skipUnless(sys.platform != "win32", "POSIX process pipe")
    def test_failed_and_oversized_verifier_output(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "nonzero"):
                adapter.run([sys.executable, "-c", "raise SystemExit(1)"], temp, 3)
            with self.assertRaisesRegex(ValueError, "output_cap"):
                adapter.run([sys.executable, "-c", "print('x' * 3000000)"], temp, 3)
