#!/usr/bin/env python3
"""Collect native GitLab pipeline evidence without writing to GitLab.

This snapshot is not merge eligibility or proof of why a job waited.
Use a read-only API token in GITLAB_TOKEN (never as an argument).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

UNKNOWN = "unknown"
PAGE_SIZE = 100
MAX_RESPONSE = 8 * 1024 * 1024
MAX_JOBS = 10_000


class Unavailable(Exception):
    """An endpoint could not be observed with the requested coverage."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api_base(server: str) -> str:
    parsed = urllib.parse.urlsplit(server)
    if (not server.isascii() or any(c.isspace() or ord(c) < 33 for c in server)
            or parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or "\\" in server or parsed.port == 0
            or any(part in {".", ".."} for part in urllib.parse.unquote(parsed.path).split("/"))):
        raise ValueError("server must be an HTTPS GitLab origin with optional instance path")
    return server.rstrip("/") + "/api/v4"


class Client:
    def __init__(self, server, token=None, timeout=10, budget=60, opener=None):
        self.base = api_base(server)
        self.timeout = timeout
        self.deadline = time.monotonic() + budget
        self.headers = {"Accept": "application/json"}
        if token:
            self.headers["PRIVATE-TOKEN"] = token
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def get(self, path, params=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise Unavailable("collection budget expired")
        query = "?" + urllib.parse.urlencode(params) if params else ""
        request = urllib.request.Request(self.base + path + query, headers=self.headers, method="GET")
        try:
            with self.opener.open(request, timeout=min(self.timeout, remaining)) as response:
                raw = bytearray()
                while len(raw) <= MAX_RESPONSE:
                    if time.monotonic() >= self.deadline:
                        raise Unavailable("collection budget expired")
                    block = response.read1(min(64 * 1024, MAX_RESPONSE + 1 - len(raw)))
                    if not block:
                        break
                    raw.extend(block)
                if len(raw) > MAX_RESPONSE:
                    raise Unavailable("response size bound reached")
                return json.loads(raw), response.headers
        except urllib.error.HTTPError as exc:
            exc.close()
            raise Unavailable(f"HTTP {exc.code}") from None
        except (OSError, ValueError, UnicodeError, RecursionError) as exc:
            raise Unavailable(f"invalid or unavailable response ({type(exc).__name__})") from None

    def pages(self, path, max_pages, **params):
        rows, seen = [], set()
        for page in range(1, max_pages + 1):
            batch, headers = self.get(path, {**params, "page": page, "per_page": PAGE_SIZE})
            if not isinstance(batch, list) or len(batch) > PAGE_SIZE:
                raise Unavailable("invalid list page")
            for row in batch:
                if not isinstance(row, dict) or not identity(row.get("id")) or row["id"] in seen:
                    raise Unavailable("duplicate or malformed item across pages")
                seen.add(row["id"])
            rows.extend(batch)
            next_page = headers.get("X-Next-Page")
            link_next = 'rel="next"' in headers.get("Link", "")
            if next_page and next_page != str(page + 1):
                raise Unavailable("unexpected pagination pointer")
            if not next_page and not link_next and len(batch) < PAGE_SIZE:
                total = headers.get("X-Total")
                if total and (not total.isdecimal() or len(total) > 10 or int(total) != len(rows)):
                    raise Unavailable("pagination count mismatch")
                return rows
        raise Unavailable("pagination bound reached before completeness")


def identity(value):
    return type(value) is int and 0 < value <= 2**63 - 1


def fields(row, *keys):
    # Retain only the scalar fields in this evidence contract, never nested
    # job payloads such as commit authors, variables, users or runner metadata.
    result = {}
    for key in keys:
        value = row.get(key)
        result[key] = value if (
            isinstance(value, str) and len(value) <= 2048
            or type(value) is bool
            or identity(value)
        ) else UNKNOWN
    return result


def seconds(value):
    return (value if type(value) in (float, int) and 0 <= value <= 10**9
            and math.isfinite(value) else UNKNOWN)


def downstream_project(base, child):
    if identity(child.get("project_id")):
        return str(child["project_id"])
    # GitLab's documented bridge response may omit project_id. Use the
    # same-instance web URL only as metadata, never as a request target.
    url = child.get("web_url")
    if not isinstance(url, str) or len(url) > 2048:
        return None
    try:
        dst, src = urllib.parse.urlsplit(url), urllib.parse.urlsplit(base)
        if (dst.scheme != src.scheme or dst.hostname != src.hostname
                or (dst.port or 443) != (src.port or 443) or dst.username is not None
                or dst.password is not None or dst.query or dst.fragment):
            return None
        prefix = src.path.removesuffix("/api/v4")
        path = urllib.parse.unquote(dst.path, errors="strict")
        if not path.startswith(prefix + "/"):
            return None
        route = re.fullmatch(r"/(.+?)/(?:-/)?pipelines/([0-9]+)", path[len(prefix):])
        if not route or route[2] != str(child["id"]):
            return None
        project = route[1]
        parts = project.split("/")
        if len(parts) >= 2 and all(re.fullmatch(r"[A-Za-z0-9_.-]+", x) and x not in {".", "..", "-"}
                                   for x in parts):
            return project
    except (ValueError, UnicodeError):
        pass
    return None


def collect(client, project, pipeline, max_pages=10, max_pipelines=12):
    queue = [(project, pipeline)]
    seen = set()
    aliases = set()
    pipelines, gaps = [], []
    job_budget = MAX_JOBS
    while queue and len(seen) < max_pipelines:
        key = queue.pop(0)
        if key in seen or key in aliases:
            continue
        seen.add(key)
        owner, pipeline_id = key
        path = f"/projects/{urllib.parse.quote(owner, safe='')}/pipelines/{pipeline_id}"

        def observe(suffix, **params):
            try:
                if not suffix:
                    return client.get(path)[0]
                return client.pages(path + suffix, max_pages, **params)
            except Unavailable as exc:
                gaps.append({"project": owner, "pipeline_id": pipeline_id,
                             "endpoint": suffix or "/pipeline", "reason": str(exc)})
                return UNKNOWN

        meta = observe("")
        if meta != UNKNOWN and (not isinstance(meta, dict) or not identity(meta.get("id"))
                                or meta["id"] != pipeline_id
                                or (owner.isascii() and owner.isdecimal()
                                    and str(meta.get("project_id")) != owner)):
            gaps.append({"project": owner, "pipeline_id": pipeline_id, "reason": "pipeline identity mismatch"})
            meta = UNKNOWN
        if meta == UNKNOWN:
            pipelines.append({"project": owner, "pipeline_id": pipeline_id,
                              "pipeline": UNKNOWN, "jobs": UNKNOWN, "bridges": UNKNOWN})
            continue
        if identity(meta.get("project_id")):
            aliases.add((str(meta["project_id"]), pipeline_id))
        all_jobs = observe("/jobs", include_retried="true")
        current = observe("/jobs", include_retried="false")
        bridges = observe("/bridges")
        latest_ids = {job["id"] for job in current} if current != UNKNOWN else None
        jobs = UNKNOWN
        if all_jobs != UNKNOWN and len(all_jobs) > job_budget:
            gaps.append({"project": owner, "pipeline_id": pipeline_id, "reason": "snapshot job cap reached"})
            all_jobs = UNKNOWN
        if all_jobs != UNKNOWN:
            job_budget -= len(all_jobs)
            if latest_ids is not None:
                by_id = {job["id"]: job for job in all_jobs}
                # Separate API reads aren't atomic. Disagreement invalidates classification.
                if any(j["id"] not in by_id or any(j.get(k) != by_id[j["id"]].get(k)
                       for k in ("status", "started_at", "finished_at")) for j in current):
                    gaps.append({"project": owner, "pipeline_id": pipeline_id,
                                 "reason": "current and retry-inclusive job reads disagree"})
                    latest_ids = None
            jobs = [
                {
                    **fields(job, "id", "name", "stage", "status", "created_at", "started_at",
                             "finished_at", "allow_failure", "failure_reason"),
                    "duration_s": seconds(job.get("duration")),
                    "queued_duration_s": seconds(job.get("queued_duration")),
                    "attempt_role": UNKNOWN if latest_ids is None else (
                        "current" if job["id"] in latest_ids else "retried"
                    ),
                }
                for job in all_jobs
            ]
        edges = UNKNOWN
        if bridges != UNKNOWN:
            edges = []
            for bridge in bridges:
                child = bridge.get("downstream_pipeline")
                edge = {**fields(bridge, "id", "name", "stage", "status"), "downstream": UNKNOWN}
                target = (downstream_project(client.base, child)
                          if isinstance(child, dict) and identity(child.get("id")) else None)
                if target is not None:
                    edge["downstream"] = fields(child, "id", "project_id", "sha", "ref", "status")
                    edge["downstream"]["project"] = target
                    queued = (target, child["id"])
                    if queued not in seen and queued not in aliases and queued not in queue:
                        queue.append(queued)
                elif child is not None or bridge.get("status") not in ("skipped", "manual", "canceled"):
                    gaps.append({"project": owner, "pipeline_id": pipeline_id,
                                 "reason": f"downstream identity unavailable for bridge {bridge['id']}"})
                edges.append(edge)
        pipelines.append({
            "project": owner,
            "pipeline_id": pipeline_id,
            "pipeline": fields(meta, "id", "project_id", "sha", "ref", "source", "status",
                               "created_at", "updated_at", "started_at", "finished_at"),
            "jobs": jobs,
            "bridges": edges,
        })
    if queue:
        gaps.append({"reason": "downstream pipeline collection cap reached", "remaining": len(queue)})
    return {
        "schema": "ci-gitlab-pipeline-snapshot/1",
        "provider": "gitlab",
        "source_api": client.base,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "root": {"project": project, "pipeline_id": pipeline},
        "complete": not gaps,
        "gaps": gaps,
        "pipelines": pipelines,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="https://gitlab.com")
    parser.add_argument("--project", required=True, help="numeric ID or unescaped namespace/project path")
    parser.add_argument("--pipeline", type=int, required=True)
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--max-pipelines", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--budget", type=float, default=60)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if not (identity(args.pipeline) and 1 <= args.max_pages <= 25 and 1 <= args.max_pipelines <= 30
            and 0 < args.timeout <= 30 and 0 < args.budget <= 180
            and 0 < len(args.project) <= 512 and all(part not in {"", ".", ".."}
            for part in args.project.split("/")) and all(32 < ord(c) != 127 for c in args.project)):
        parser.error("invalid pipeline, project or collection bounds")
    try:
        client = Client(args.server, os.environ.get("GITLAB_TOKEN"), args.timeout, args.budget)
        result = collect(client, args.project, args.pipeline, args.max_pages, args.max_pipelines)
        output = json.dumps(result, indent=2, allow_nan=False) + "\n"
        if args.out:
            with args.out.open("x", encoding="utf-8") as fp:
                fp.write(output)
        else:
            print(output, end="")
        return 0 if result["complete"] else 2
    except (OSError, ValueError) as exc:
        print(f"collection could not be written or configured ({type(exc).__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
