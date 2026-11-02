# CI optimization brief for a coding agent

Paste this to an agent that does not have the skill installed. Fill the brackets.

---

You are auditing CI for `[OWNER/REPO]` at `[path]`. Goal: shorter push → trustworthy green → merge time. A pipeline that can go green on broken code is worse than a slow one.

Mode: **OBSERVE** — read-only until I approve a change. Do not buy runners, change branch protection, secrets, permissions, or required checks, merge, deploy, or run code from untrusted PRs.

Steps:
1. Read `AGENTS.md`/`CLAUDE.md`/`CONTRIBUTING`/`CODEOWNERS` and every CI config. Note conflicts; the stricter rule wins.
2. Inventory: CI provider, languages, lockfiles, monorepo graph, test runners, required checks, privileged triggers (`pull_request_target`, `workflow_run`, deploys), secret **names** only.
3. Baseline from the provider's API: last ~150 completed runs; p50/p95 of total time, queue, setup, each job; reruns; flakes. Missing data is `unknown`, never 0. Give window and sample size for every number.
4. Build the job DAG from `needs`; name the critical path.
5. Score seven modules — fast runners, native lint/typecheck (JS/TS only), bigger machines/test-worker reuse, caching/blobless clone, parallel jobs, affected tasks (only with a real project graph), pre-push hooks — each as applicable / not, expected saving, confidence, risk, effort, cost, next proof.
6. Hunt false greens: `continue-on-error`, `|| true`, required workflows behind `paths:` (stay Pending), required jobs behind `if:` (report Success when skipped), missing `merge_group` with a merge queue.
7. Report: top bottleneck, ordered one-intervention-per-PR plan with acceptance criteria and rollback, risks, and explicit NOT APPLICABLE findings. Stop there.

When approved to implement: one module per PR; keep required check names; prove each changed gate can go red with a probe commit; ≥10 runs per arm before claiming a speed-up.
