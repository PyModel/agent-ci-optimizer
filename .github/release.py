#!/usr/bin/env python3
"""Repository-only release gates. No retries; only publish mode can write GitHub.

This is trusted main-branch release plumbing, never an installed skill command.
Every API operation is bounded and explicit. No downloaded code/artifacts run.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

REPOSITORY = "PyModel/ci-optimiser"
REPOSITORY_ID = 1442466406
ROOT = Path(__file__).resolve().parent.parent
SHA = re.compile(r"[0-9a-f]{40}\Z")
TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
WORKFLOW_PATH = ".github/workflows/ci.yml"
REQUIRED_JOBS = {
    "test (3.10)": {"Checkout repository", "Set up Python", "Unit + packaging tests"},
    "test (3.x)": {"Checkout repository", "Set up Python", "Unit + packaging tests"},
    "workflow-lint": {"Checkout repository", "Install verified actionlint", "Validate workflows"},
    "ci": {"Checkout repository", "Require success", "Read job timing evidence", "Enforce job budgets"},
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def positive_int(value):
    return type(value) is int and value > 0


def repository(value):
    require(isinstance(value, dict), "missing repository identity")
    require(type(value.get("id")) is int and value["id"] == REPOSITORY_ID
            and value.get("full_name") == REPOSITORY, "wrong repository identity")


def validate_run(run, sha, run_id, workflow_id):
    require(isinstance(run, dict), "missing CI run")
    repository(run.get("repository"))
    repository(run.get("head_repository"))
    require(positive_int(run_id) and type(run.get("id")) is int and run["id"] == run_id,
            "wrong CI run ID")
    paths = {WORKFLOW_PATH, WORKFLOW_PATH + "@main", WORKFLOW_PATH + "@refs/heads/main",
             WORKFLOW_PATH + "@" + sha}
    require(positive_int(workflow_id) and type(run.get("workflow_id")) is int
            and run["workflow_id"] == workflow_id and run.get("path") in paths
            and run.get("name") == "ci", "wrong CI workflow identity")
    require(run.get("event") == "push" and run.get("head_branch") == "main"
            and run.get("head_sha") == sha, "CI is not the exact main push")
    require(type(run.get("run_attempt")) is int and run["run_attempt"] == 1,
            "retried CI cannot authorize a release")
    require(run.get("status") == "completed" and run.get("conclusion") == "success",
            "CI did not complete successfully")


def validate_jobs(payload, sha, run_id):
    require(isinstance(payload, dict) and isinstance(payload.get("jobs"), list),
            "missing CI jobs")
    jobs = payload["jobs"]
    require(type(payload.get("total_count")) is int
            and payload["total_count"] == len(jobs) == len(REQUIRED_JOBS),
            "missing, extra or truncated CI jobs")
    require({job.get("name") for job in jobs if isinstance(job, dict)} == set(REQUIRED_JOBS),
            "CI job inventory differs from the complete required suite")
    for job in jobs:
        require(type(job.get("run_id")) is int and job["run_id"] == run_id
                and type(job.get("run_attempt")) is int and job["run_attempt"] == 1
                and job.get("head_sha") == sha, "CI job source/attempt differs")
        require(job.get("status") == "completed" and job.get("conclusion") == "success",
                "a required CI job failed or did not complete")
        steps = job.get("steps")
        require(isinstance(steps, list) and bool(steps)
                and all(isinstance(step, dict) for step in steps), "missing CI steps")
        names = [step.get("name") for step in steps]
        require(all(isinstance(name, str) for name in names) and len(names) == len(set(names))
                and REQUIRED_JOBS[job["name"]].issubset(names), "missing or duplicate CI steps")
        require(all(step.get("status") == "completed" and step.get("conclusion") == "success"
                    for step in steps), "a CI step was skipped, failed or incomplete")


def validate_tag(tag):
    match = TAG.fullmatch(tag)
    require(match is not None and tuple(map(int, match.groups())) > (0, 1, 0),
            "expected a stable version newer than preserved v0.1.0")


def validate_release(release, tag, sha, *, draft, release_id=None):
    require(isinstance(release, dict) and positive_int(release.get("id")), "invalid release")
    require(release_id is None or release["id"] == release_id, "release ID changed")
    require(release.get("tag_name") == tag and release.get("target_commitish") == sha,
            "release source differs from verified main")
    require(release.get("draft") is draft and release.get("prerelease") is False,
            "unexpected release state")
    require(release.get("assets") == [], "source-only release has unexpected assets")


def validate_versions(root, tag):
    version = tag.removeprefix("v")
    require((root / "version.txt").read_text().strip() == version, "version.txt differs from tag")
    require(json.loads((root / ".release-please-manifest.json").read_text()) == {".": version},
            "release manifest differs from tag")
    changelog = (root / "CHANGELOG.md").read_text()
    require(re.search(r"(?m)^## (?:\[)?" + re.escape(version) + r"(?:\]|\s|$)", changelog),
            "changelog has no matching release entry")
    bundle = root / "skills/ci-optimiser"
    require((bundle / "SKILL.md").is_file() and (bundle / "LICENSE").is_file(),
            "installable skill entrypoint/license missing")


def command(argv):
    return subprocess.run(argv, check=True, capture_output=True, text=True,
                          timeout=15, cwd=ROOT).stdout


def api(path, *, data=None):
    argv = ["gh", "api", "--method", "GET" if data is None else "PATCH",
            "-H", "Accept: application/vnd.github+json", "-H", "X-GitHub-Api-Version: 2022-11-28",
            f"repos/{REPOSITORY}" + (f"/{path}" if path else "")]
    if data is not None:
        # The only allowed write is publishing an already verified draft.
        require(re.fullmatch(r"releases/[1-9][0-9]*", path) is not None
                and data == {"draft": False}, "unsupported release write")
        argv.extend(["-F", "draft=false"])
    return json.loads(command(argv))


def require_current_main(sha):
    ref = api("git/ref/heads/main")
    require(isinstance(ref, dict) and ref.get("ref") == "refs/heads/main"
            and ref.get("object", {}).get("type") == "commit"
            and ref["object"].get("sha") == sha, "stale CI event: main moved; no release mutation allowed")


def verify_ci(sha, run_id, event_path=None):
    identity = api("")
    repository(identity)
    require(identity.get("default_branch") == "main", "unexpected default branch")
    workflow = api("actions/workflows/ci.yml")
    require(isinstance(workflow, dict) and positive_int(workflow.get("id"))
            and workflow.get("path") == WORKFLOW_PATH and workflow.get("name") == "ci"
            and workflow.get("state") == "active", "required CI workflow changed")
    if event_path is not None:
        event = json.loads(event_path.read_text())
        require(isinstance(event, dict) and event.get("action") == "completed",
                "expected workflow_run completion event")
        repository(event.get("repository"))
        validate_run(event.get("workflow_run"), sha, run_id, workflow["id"])
    validate_run(api(f"actions/runs/{run_id}"), sha, run_id, workflow["id"])
    validate_jobs(api(f"actions/runs/{run_id}/attempts/1/jobs?per_page=100"), sha, run_id)
    require_current_main(sha)


def verify_release_candidates(sha):
    """Conservative superset of Release Please 17.6.0's pending-PR selection.

    Its manifest scans up to 200 merged main PRs ordered by updated time. It
    subsequently parses bodies/titles; we reject stale pending PRs even if a
    malformed body would make upstream ignore them. Closed, unmerged PRs do not
    consume that limit. Pagination is bounded and incomplete evidence fails.
    """
    config = json.loads((ROOT / "release-please-config.json").read_text())
    require(set(config) == {"$schema", "bootstrap-sha", "release-type", "include-component-in-tag",
                            "include-v-in-tag", "draft", "force-tag-creation", "packages"}
            and config.get("release-type") == "simple" and config.get("draft") is True
            and config.get("force-tag-creation") is True
            and config.get("include-component-in-tag") is False
            and config.get("include-v-in-tag") is True
            and config.get("packages") == {".": {"package-name": "ci-optimiser",
                                                  "version-file": "version.txt",
                                                  "changelog-path": "CHANGELOG.md"}},
            "release candidate policy no longer matches Release Please configuration")
    merged = 0
    seen = set()
    for page in range(1, 11):
        pulls = api("pulls?state=closed&base=main&sort=updated&direction=desc"
                    f"&per_page=100&page={page}")
        require(isinstance(pulls, list) and len(pulls) <= 100, "invalid release PR page")
        for pull in pulls:
            require(isinstance(pull, dict) and positive_int(pull.get("number"))
                    and pull["number"] not in seen, "duplicate or invalid release PR evidence")
            seen.add(pull["number"])
            require(pull.get("state") == "closed" and isinstance(pull.get("base"), dict)
                    and pull["base"].get("ref") == "main" and "merged_at" in pull,
                    "release PR enumeration changed scope")
            repository(pull["base"].get("repo"))
            if pull.get("merged_at") is None:
                continue
            require(isinstance(pull.get("merged_at"), str) and bool(pull["merged_at"]),
                    "unknown merge status")
            require(isinstance(pull.get("merge_commit_sha"), str)
                    and SHA.fullmatch(pull["merge_commit_sha"]) is not None, "unknown merged PR source")
            merged += 1
            labels = pull.get("labels")
            require(isinstance(labels, list)
                    and all(isinstance(label, dict) and isinstance(label.get("name"), str)
                            for label in labels), "missing release PR labels")
            if any(label["name"] == "autorelease: pending" for label in labels):
                require(pull.get("merge_commit_sha") == sha,
                        "pending release PR source is not current verified main; reconcile it before release")
            if merged == 200:
                return
        if len(pulls) < 100:
            return
    raise ValueError("release PR enumeration exceeded its completeness bound")


def verify_source(sha, tag):
    validate_tag(tag)
    validate_versions(ROOT, tag)
    commit = api(f"commits/{tag}")
    require(isinstance(commit, dict) and commit.get("sha") == sha, "tag does not resolve to verified source")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("gate", "prepare", "verify", "publish"))
    parser.add_argument("--sha", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--tag")
    parser.add_argument("--release-sha")
    parser.add_argument("--release-id", type=int)
    args = parser.parse_args(argv)
    write_attempted = False
    write_confirmed = False
    try:
        require(SHA.fullmatch(args.sha) is not None and positive_int(args.run_id), "invalid source identity")
        require(os.environ.get("GITHUB_REPOSITORY") == REPOSITORY
                and os.environ.get("GITHUB_REPOSITORY_ID") == str(REPOSITORY_ID)
                and os.environ.get("GITHUB_EVENT_NAME") == "workflow_run"
                and os.environ.get("GITHUB_RUN_ATTEMPT") == "1", "untrusted or rerun release context")
        require(command(["git", "rev-parse", "HEAD"]).strip() == args.sha, "checkout is not the verified source")
        event_path = Path(os.environ["GITHUB_EVENT_PATH"])
        verify_ci(args.sha, args.run_id, event_path)
        if args.mode in ("gate", "prepare"):
            if args.mode == "prepare":
                verify_release_candidates(args.sha)
                require_current_main(args.sha)
            print(f"Verified complete first-attempt main CI for {args.sha}")
            return 0
        require(isinstance(args.tag, str) and args.release_sha == args.sha,
                "Release Please source differs from the triggering verified main commit")
        if args.mode == "publish":
            require(positive_int(args.release_id), "publish requires the verified release ID")
        verify_source(args.sha, args.tag)
        if args.mode == "verify":
            print(f"Verified release source {args.tag} at {args.sha}")
            return 0
        # Draft metadata requires push access. Only the trusted publisher reads
        # it; package verification keeps its contents:read token. Use the exact
        # Release Please ID because the by-tag route is for published releases.
        release = api(f"releases/{args.release_id}")
        validate_release(release, args.tag, args.sha, draft=True, release_id=args.release_id)
        # Re-read before mutation. GitHub has no atomic compare-main/tag-and-
        # publish API; postchecks detect observed concurrent writes, not prevent them.
        require_current_main(args.sha)
        require(api(f"commits/{args.tag}").get("sha") == args.sha, "tag changed before publication")
        write_attempted = True
        published = api(f"releases/{release['id']}", data={"draft": False})
        validate_release(published, args.tag, args.sha, draft=False, release_id=release["id"])
        write_confirmed = True
        validate_release(api(f"releases/{release['id']}"), args.tag, args.sha,
                         draft=False, release_id=release["id"])
        require(api(f"commits/{args.tag}").get("sha") == args.sha, "tag changed during publication")
        require_current_main(args.sha)
        print(f"Published {args.tag} from verified source {args.sha}")
        return 0
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        # Do not print subprocess stderr, response bodies or authentication data.
        message = str(error) if isinstance(error, ValueError) else type(error).__name__
        if write_confirmed:
            prefix = f"Post-publication verification failed for release ID {args.release_id}; inspect public state"
        elif write_attempted:
            prefix = f"Publication may have occurred for release ID {args.release_id}; inspect state without rerunning"
        else:
            prefix = "Release blocked before publication"
        print(f"{prefix}: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
