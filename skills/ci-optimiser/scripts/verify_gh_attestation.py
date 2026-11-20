#!/usr/bin/env python3
"""Verify local artifact bytes with a trusted, operator-pinned GitHub CLI.

This is provenance evidence only, never validation or release eligibility.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import selectors
import stat
import subprocess
import tempfile
import time
from pathlib import Path

LIMIT = 2 * 1024 * 1024
PREDICATE = "https://slsa.dev/provenance/v1"
REPO = r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+"
SHA = r"(?:[a-f0-9]{40}|[a-f0-9]{64})"


def hash_file(path: Path, limit: int, destination: Path | None = None) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("input_not_regular_file")
    h = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as src:
        if not stat.S_ISREG(os.fstat(src.fileno()).st_mode):
            raise ValueError("input_not_regular_file")
        dst = destination.open("xb") if destination else None
        try:
            remaining = limit
            while chunk := src.read(1024 * 1024):
                remaining -= len(chunk)
                if remaining < 0:
                    raise ValueError("input_size_cap")
                h.update(chunk)
                if dst:
                    dst.write(chunk)
        finally:
            if dst:
                dst.close()
    return h.hexdigest()


def run(argv: list[str], cwd: str, timeout: float) -> bytes:
    if os.name != "posix":
        raise ValueError("verifier_requires_posix")
    deadline = time.monotonic() + timeout
    env = {**os.environ, "GH_PROMPT_DISABLED": "1", "GH_HOST": "github.com"}
    with subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        assert process.stdout is not None
        try:
            with selectors.DefaultSelector() as sel:
                sel.register(process.stdout, selectors.EVENT_READ)
                output = bytearray()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not sel.select(remaining):
                        raise ValueError("verifier_timeout")
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > LIMIT:
                        raise ValueError("verifier_output_cap")
            if process.wait(timeout=max(0, deadline - time.monotonic())):
                raise ValueError("verifier_nonzero")
            return bytes(output)
        finally:
            if process.poll() is None:
                process.kill()


def check_output(raw: bytes, digest: str) -> int:
    items = json.loads(raw)
    if not isinstance(items, list) or not items or len(items) > 30:
        raise ValueError("unexpected_verification_output")
    for item in items:
        try:
            result = item["verificationResult"]
            statement = result["statement"]
            certificate = result["signature"]["certificate"]
            subjects = statement["subject"]
            if (not isinstance(certificate, dict) or not certificate
                    or statement["predicateType"] != PREDICATE
                    or not isinstance(subjects, list)
                    or not any(subject["digest"].get("sha256") == digest for subject in subjects)):
                raise ValueError("verification_subject_mismatch")
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError("unexpected_verification_output") from exc
    return len(items)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--artifact", type=Path, required=True)
    p.add_argument("--gh", type=Path, required=True, help="absolute, trusted and immutable gh binary")
    p.add_argument("--gh-sha256", required=True, help="operator-reviewed SHA256 of the gh binary")
    p.add_argument("--repo", required=True)
    p.add_argument("--signer-workflow", required=True, help="OWNER/REPO/.github/workflows/FILE.yml")
    p.add_argument("--source-digest", required=True, help="exact expected source commit")
    p.add_argument("--signer-digest", required=True, help="exact expected signer workflow revision")
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--allow-self-hosted", action="store_true")
    args = p.parse_args(argv)
    if (not re.fullmatch(REPO, args.repo)
            or not re.fullmatch(REPO + r"/\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", args.signer_workflow)
            or not all(re.fullmatch(SHA, value) for value in (args.source_digest, args.signer_digest))
            or not re.fullmatch(r"[a-f0-9]{64}", args.gh_sha256)
            or not args.gh.is_absolute() or not 1 <= args.timeout <= 120):
        p.error("invalid identity, digest, absolute verifier path or timeout")
    out = {"schema": "ci-github-attestation/1", "status": "unknown", "release_eligibility": "unknown"}
    try:
        if hash_file(args.gh, 128 * 1024 * 1024) != args.gh_sha256:
            raise ValueError("verifier_digest_mismatch")
        with tempfile.TemporaryDirectory(prefix="ci-attestation-") as cwd:
            artifact = Path(cwd) / "artifact.bin"
            digest = hash_file(args.artifact, 2 * 1024**3, artifact)
            cmd = [str(args.gh), "attestation", "verify", str(artifact), "--hostname", "github.com",
                   "--repo", args.repo, "--signer-workflow", args.signer_workflow,
                   "--source-digest", args.source_digest, "--signer-digest", args.signer_digest,
                   "--cert-oidc-issuer", "https://token.actions.githubusercontent.com",
                   "--predicate-type", PREDICATE, "--digest-alg", "sha256", "--format", "json", "--limit", "30"]
            if not args.allow_self_hosted:
                cmd.append("--deny-self-hosted-runners")
            raw = run(cmd, cwd, args.timeout)
            count = check_output(raw, digest)
        out.update(status="verified", artifact_sha256=digest, repo=args.repo,
                   signer_workflow=args.signer_workflow, source_digest=args.source_digest,
                   signer_digest=args.signer_digest, verifier_sha256=args.gh_sha256,
                   allow_self_hosted=args.allow_self_hosted,
                   verified_count=count, verifier_output_sha256=hashlib.sha256(raw).hexdigest())
    except (ValueError, OSError, RecursionError, subprocess.TimeoutExpired) as exc:
        # Don't echo verifier output, paths, or arbitrary JSON parsing diagnostics.
        out["reason"] = str(exc) if type(exc) is ValueError else "verifier_unavailable_or_invalid"
    print(json.dumps(out, sort_keys=True))
    return 0 if out["status"] == "verified" else 3


if __name__ == "__main__":
    raise SystemExit(main())
