#!/usr/bin/env python3
"""Require every expected shard artifact before staging JSON reports for merging.

Download artifacts from the exact workflow run/attempt into separate directories
(for example blob-1, blob-2). This validates transport completeness, not test or
coverage success. The original test runner must merge and enforce those gates.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path


def inspect(root, expected, prefix="blob-", pattern="blob-*.json", max_bytes=64 * 1024 * 1024):
    errors, payloads = [], {}
    if root.is_symlink() or not root.is_dir():
        return ["report root must be a real directory"], {}
    if not expected or len(set(expected)) != len(expected) or not all(re.fullmatch(r"[A-Za-z0-9_-]+", x) for x in expected):
        return ["expected shard IDs must be unique safe names"], {}
    if not re.fullmatch(r"[A-Za-z0-9_-]*", prefix) or not pattern or "/" in pattern or "\\" in pattern:
        return ["prefix/pattern must be a single safe filename component"], {}
    wanted = {prefix + shard for shard in expected}
    actual = {path.name for path in root.iterdir() if path.name.startswith(prefix)}
    if actual != wanted:
        errors.append(f"shard directories differ: missing={sorted(wanted-actual)}, unexpected={sorted(actual-wanted)}")
    for shard_ordinal, directory in enumerate(sorted(wanted & actual), 1):
        folder = root / directory
        if folder.is_symlink() or not folder.is_dir():
            errors.append(f"invalid shard directory: {directory}")
            continue
        found = 0
        def traversal_error(_exc):
            errors.append(f"unreadable directory in shard: {directory}")

        for base, dirs, files in os.walk(folder, followlinks=False, onerror=traversal_error):
            for name in list(dirs):
                if (Path(base) / name).is_symlink():
                    errors.append(f"symlink directory in shard: {directory}")
                    dirs.remove(name)
            for name in sorted(files):
                if not fnmatch.fnmatchcase(name, pattern):
                    continue
                path = Path(base) / name
                relative = path.relative_to(folder).as_posix()
                try:
                    if not hasattr(os, "O_NOFOLLOW"):
                        raise ValueError("safe no-follow reads unsupported on this platform")
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
                    with os.fdopen(fd, "rb") as stream:
                        metadata = os.fstat(stream.fileno())
                        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= max_bytes:
                            raise ValueError("report is empty, oversized or non-regular")
                        data = stream.read(max_bytes + 1)
                    if len(data) > max_bytes:
                        raise ValueError("report grew beyond size limit")
                    parsed = json.loads(data)
                    if not isinstance(parsed, (dict, list)) or not parsed:
                        raise ValueError("report JSON has no structured payload")
                    # Prefixing prevents equal report basenames from overwriting
                    # one another when the test runner consumes one merge directory.
                    key = f"blob-{shard_ordinal:06d}-{found + 1:06d}.json"
                    payloads[key] = data
                    found += 1
                except (OSError, ValueError, UnicodeError) as exc:
                    errors.append(f"{directory}/{relative}: {type(exc).__name__}")
        if not found:
            errors.append(f"no valid matching report for shard: {directory}")
    return errors, payloads


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--expected-shards", required=True, nargs="+")
    parser.add_argument("--prefix", default="blob-")
    parser.add_argument("--pattern", default="blob-*.json")
    parser.add_argument("--max-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--stage-out", type=Path, help="New directory; never overwrite prior reports")
    args = parser.parse_args(argv)
    if args.max_bytes < 1:
        parser.error("--max-bytes must be positive")
    try:
        errors, payloads = inspect(args.root, args.expected_shards, args.prefix, args.pattern, args.max_bytes)
        if errors:
            print(json.dumps({"state": "blocked", "errors": errors}))
            return 1
        if args.stage_out:
            if args.stage_out.exists() or args.stage_out.is_symlink():
                raise ValueError("staging destination already exists")
            args.stage_out.parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix=".reports-", dir=args.stage_out.parent))
            try:
                for name, data in payloads.items():
                    (temporary / name).write_bytes(data)
                temporary.rename(args.stage_out)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        print(json.dumps({"state": "complete", "shards": len(args.expected_shards), "reports": len(payloads),
                          "scope": "transport only; merge/tests/coverage still required"}))
        return 0
    except (OSError, ValueError) as exc:
        print(f"report validation error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
