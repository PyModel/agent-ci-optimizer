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
COUNTERS = ("requests", "retries", "conditional_hits", "cache_write_errors")


def timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is not None
    except ValueError:
        return False


def transport_policy(get):
    """Project only bounded public transport settings; never arbitrary attributes."""
    values = {}
    for output, attribute, upper in (("timeout_s", "timeout", 86400),
                                     ("max_wait_s_per_request", "max_wait", 30)):
        value = getattr(get, attribute, None)
        valid = type(value) in (int, float) and 0 <= value <= upper and (attribute != "timeout" or value > 0)
        values[output] = value if valid else None
    retries = getattr(get, "retries", None)
    values["retries_per_request"] = retries if type(retries) is int and 0 <= retries <= 5 else None
    return values


def counter_snapshot(get):
    stats = getattr(get, "stats", None)
    stats = stats if isinstance(stats, dict) else {}
    return {key: stats[key] if type(stats.get(key)) is int and stats[key] >= 0 else None for key in COUNTERS}


def transport_report(get, before, policy, calls):
    after = counter_snapshot(get)
    retries = policy["retries_per_request"]
    limits = {"requests": calls * ((retries if retries is not None else 5) + 1),
              "retries": calls * (retries if retries is not None else 5),
              "conditional_hits": calls, "cache_write_errors": calls}
    counters = {}
    for key in COUNTERS:
        value = after[key] - before[key] if before[key] is not None and after[key] is not None else None
        counters[key] = value if value is not None and 0 <= value <= limits[key] else None
    # Reject contradictory counters, not just individually bounded numbers.
    if counters["requests"] is not None and counters["retries"] is not None:
        if counters["requests"] != calls + counters["retries"]:
            counters["requests"] = counters["retries"] = None
    return {"policy": policy, "observed_api_calls": calls, "stats": counters,
            "counters_complete": all(value is not None for value in counters.values()),
            "scope": "This inventory collection only; metadata GET attempts, not workflow or test retries."}


def collect(get, repo, pages):
    if (not isinstance(repo, str) or not REPO.fullmatch(repo) or repo.split("/")[1] in (".", "..")
            or type(pages) is not int or not 1 <= pages <= MAX_PAGES):
        raise ValueError("valid OWNER/REPO and 1..20 pages required")
    policy, before, calls = transport_policy(get), counter_snapshot(get), 0
    policy["max_pages"] = pages
    out = {"schema": "ci-github-cache-inventory/1", "repository": repo,
           "started_at": utc_now(), "complete": False, "gaps": [], "caches": [],
           "provider_reported_count": None, "complete_inventory_bytes": None,
           "limits": ["Non-atomic metadata observations, not individual restore, hit or save events.",
                      "Keys and versions are hashed; low-entropy values are not anonymized."]}
    seen = set()
    try:
        for page in range(1, pages + 1):
            query = urlencode({"per_page": PAGE_SIZE, "page": page, "sort": "created_at", "direction": "asc"})
            calls += 1
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
    out["transport"] = transport_report(get, before, policy, calls)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="OWNER/REPO; authenticated gh on github.com")
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--retries", type=int, default=0,
                        help="metadata GET retries per request, 0..5; strict default 0, never retries CI")
    args = parser.parse_args(argv)
    if not REPO.fullmatch(args.repo) or args.repo.split("/")[1] in (".", ".."):
        parser.error("--repo must be OWNER/REPO")
    if not 1 <= args.max_pages <= MAX_PAGES:
        parser.error("--max-pages must be 1..20")
    if not 0 <= args.retries <= 5:
        parser.error("--retries must be 0..5")
    report = collect(GhClient(timeout=10, retries=args.retries, max_wait=2 if args.retries else 0),
                     args.repo, args.max_pages)
    print(json.dumps(report, allow_nan=False, sort_keys=True))
    return 0 if report["complete"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
