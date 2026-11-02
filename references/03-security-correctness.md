# Security and correctness

A faster pipeline that can go green on broken code is a regression. Check every proposed change against this file before it reaches a PR.

## Trust zones

| Zone | Examples | Secrets | Write token | Cache |
|---|---|---|---|---|
| Untrusted | fork `pull_request`, PRs from outside contributors | none | read-only | restore only |
| Trusted PR | same-repo branch PR | scoped, per job | minimal | own branch scope |
| Default branch | `push` to main, `schedule` | as needed | as needed | writes the shared cache |
| Privileged | release, deploy, `pull_request_target`, `workflow_run` | yes | yes | never restores caches untrusted code wrote |

Rules: set `permissions:` at workflow top to `contents: read` and widen per job (OIDC `id-token: write` only on the job that exchanges the token, never workflow-wide); keep deploy and release in separate jobs (or workflows) gated by environment protection.

## Privileged triggers

`pull_request_target` runs with the base repo's `GITHUB_TOKEN` and repository/org secrets; the workflow file and a default `actions/checkout` come from the default branch. Checking out the PR head and running its code (install scripts, tests, build) is the "pwn request" pattern: it hands secrets and write access to the PR author and can poison caches. `actions/checkout` v7 refuses to check out fork PR code in `pull_request_target`/`workflow_run` unless `allow-unsafe-pr-checkout: true`; treat that flag as a stop-and-escalate signal.

Safe shape: untrusted code runs in a `pull_request` workflow with no secrets and uploads an artifact; a separate privileged workflow consumes the artifact as data, never executes it.

## Required checks and skips

GitHub docs (2026-11-01):
- A **workflow** skipped by `paths`, `branches`, or commit-message filters leaves its required checks **Pending** and blocks merging. GitHub's advice: do not require workflows that can be skipped.
- A **job** skipped by `if:` reports **Success**.

Consequences:
- Moving a required workflow behind `paths:` blocks unrelated PRs forever. Moving a required job behind `if:` can silently pass. Neither is safe without an aggregate job.
- Aggregate pattern: one always-running job (`if: always()`) that `needs` every gated job and fails unless each need's `result` is `success` or an **intended** `skipped` (decide per need, in code).
- Renaming a job renames its check context. Keep names, or update branch protection/rulesets in the same change with owner approval.
- Merge queue: workflows must also trigger on `merge_group`, or required checks never report for queued PRs.

## False-green patterns to hunt

`continue-on-error: true`, `|| true`, `set +e`, `exit 0` in traps, test commands with `--passWithNoTests`, `--bail` combined with ignored exit codes, coverage thresholds lowered, `if:` conditions that are always false on PRs, selectors with empty output treated as "nothing to test", matrix `fail-fast: false` with no aggregate, tests filtered by `.only`/`skip` committed by accident.

## Failure testing (do it, don't argue it)

For any change to gating, selection or parallel structure, prove the pipeline goes **red**:

1. Commit a deliberately failing test in each affected area → required check fails.
2. Break lint and types separately → each fails.
3. For affected selection: change a lockfile, a root config, a CI file, a schema, and a cross-package rename → FULL.
4. Cancel one parallel job → aggregate fails.
5. Delete the cache → run still green and correct (cold path).

Record each as an experiment entry; revert the probes.

## Cache poisoning

A cache key an untrusted run can write and a trusted run can restore is an injection path. GitHub scopes PR caches to the PR merge ref, which protects the default branch; self-hosted or third-party cache backends may not. Never restore build outputs (as opposed to package stores) into release jobs; build releases from a clean install.

## Supply chain

Pin third-party actions to a full commit SHA with a version comment; let Dependabot/Renovate bump them. Pin container images by digest in privileged jobs. A new linter, runner vendor, or hook manager is a new dependency: name its maintainer, license, and install scripts in the PR.

## Before publishing a report

Review the findings as if each one were wrong, then keep only what survives:

1. Re-derive every number from `metrics.json` or the raw data with exact job names; numbers in prose match the tables.
2. Each quoted "before" line exists in the real file, composite actions and reusable workflows included.
3. Evidence shows the aggregate (p50/p95 with n), not one cherry-picked run.
4. The saving is causal and possible: no per-change wall-clock gain above the floor or above the whole path ([sizing](04-measurement-benchmarking.md#sizing-and-combining-savings)).
5. The remedy is a change to CI config or tooling someone can make, not "fix your code".
6. Intent: `git log --follow -p` and the PR that added the line. A recent, deliberate choice the fix contradicts is a policy change for the owner, not a quick win.

When a second, independent reviewer is available (a fresh agent given the repo and the report, not your conclusions), cut any finding only one of you would defend.
