#!/usr/bin/env python3
"""Read-only GitHub cache inventory; it cannot establish per-run cache hits."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from urllib.parse import urlencode

try:
    from .collect_gh import GhClient, GhError, identity, utc_now
except ImportError:
    from collect_gh import GhClient, GhError, identity, utc_now

REPO = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+")
MAX_PAGES = 20
PAGE_SIZE = 100


def timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is not None
    except ValueError:
        return False


def collect(get, repo, pages):
    out = {"schema": "ci-github-cache-inventory/1", "repository": repo,
           "started_at": utc_now(), "complete": False, "gaps": [], "caches": [],
           "provider_reported_count": None, "complete_inventory_bytes": None,
           "limits": ["Non-atomic metadata observations, not individual restore, hit or save events.",
                      "Keys and versions are hashed; low-entropy values are not anonymized."]}
    seen = set()
    try:
        for page in range(1, pages + 1):
            query = urlencode({"per_page": PAGE_SIZE, "page": page, "sort": "created_at", "direction": "asc"})
            data = get(f"repos/{repo}/actions/caches?{query}")
            total = data.get("total_count") if isinstance(data, dict) else None
            rows = data.get("actions_caches") if isinstance(data, dict) else None
            if type(total) is not int or total < 0 or not isinstance(rows, list) or len(rows) > PAGE_SIZE:
                out["gaps"].append("invalid_page")
                break
            if out["provider_reported_count"] is None:
                out["provider_reported_count"] = total
            elif total != out["provider_reported_count"]:
                out["gaps"].append("inventory_changed_during_collection")
                break
            if len(rows) != min(PAGE_SIZE, max(0, total - len(seen))):
                out["gaps"].append("page_count_mismatch")
                break
            for row in rows:
                if not isinstance(row, dict):
                    out["gaps"].append("invalid_cache_record")
                    break
                item = row.get("id")
                size = row.get("size_in_bytes")
                allowed = ("ref", "key", "version")
                dates = ("created_at", "last_accessed_at")
                if (not identity(item) or type(size) is not int or size < 0
                        or not all(isinstance(row.get(x), str) and 0 < len(row[x]) <= 4096 for x in allowed)
                        or not all(timestamp(row.get(x)) for x in dates)):
                    out["gaps"].append("invalid_cache_record")
                    break
                if item in seen:
                    out["gaps"].append("duplicate_cache_identity")
                    break
                seen.add(item)
                out["caches"].append({**{x: row[x] for x in (*dates, "ref")}, "id": item,
                                      "size_in_bytes": size,
                                      **{f"{x}_sha256": hashlib.sha256(row[x].encode()).hexdigest()
                                         for x in ("key", "version")}})
            if out["gaps"]:
                break
            if len(seen) == total:
                out["complete"] = True
                out["complete_inventory_bytes"] = sum(x["size_in_bytes"] for x in out["caches"])
                break
        else:
            out["gaps"].append("page_budget_reached")
    except GhError as error:
        out["gaps"].append(error.kind)
    out["finished_at"] = utc_now()
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="OWNER/REPO; authenticated gh on github.com")
    parser.add_argument("--max-pages", type=int, default=5)
    args = parser.parse_args(argv)
    if not REPO.fullmatch(args.repo) or args.repo.split("/")[1] in (".", ".."):
        parser.error("--repo must be OWNER/REPO")
    if not 1 <= args.max_pages <= MAX_PAGES:
        parser.error("--max-pages must be 1..20")
    report = collect(GhClient(timeout=10, retries=1, max_wait=2), args.repo, args.max_pages)
    print(json.dumps(report, allow_nan=False, sort_keys=True))
    return 0 if report["complete"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
