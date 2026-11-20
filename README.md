# ![Agent CI Optimizer](assets/banner.svg)

An agent skill that makes CI faster without making it lie. It audits a repo's
push → trustworthy green → merge loop, measures where the time goes, and proposes one
reversible, benchmarked change at a time across seven modules: fast runners, native
lint/typecheck, bigger machines and test-worker reuse, caching and blobless clones,
parallel jobs, affected tasks, and pre-push hooks. It starts read-only and treats a
false green as worse than a slow build.

Before tuning runners, it designs four validation categories from the repository's
actual commands and policy: **code rules**, **change correctness**, **integrated main**,
and **release readiness**. Its default targets are PR p50 <120 seconds and p95 <180
seconds. The full suite runs in a merge queue before main; selection narrows PR
feedback only. It rejects retries, skipped tests and weaker checks as optimizations,
and ties releases to verified commits and artifacts.
For services it evaluates feature gates and progressive rollouts; for libraries and
other products it uses the appropriate compatibility, packaging and staging gates.
Detection produces evidence for agent review, not permission to remove checks.

## Contents

- [SKILL.md](SKILL.md): the procedure agents follow.
- [Strict performance contract](references/14-two-minute-ci.md): exact budgets, full queue execution, honest measurements, activation blockers and cost.
- [Current toolchain migration](references/15-toolchain-migration.md): Bun, Oxlint, Oxfmt, TypeScript 7, tsrs and Vitest parity requirements.
- [references/](references/): discovery matrix, playbook, safety rules, measurement, examples, dated sources, config recipes and [four-category validation architecture](references/08-validation-architecture.md).
- [templates/](templates/): audit report, optimization PR, paste-ready brief for agents without the skill.
- [scripts/](scripts/): Python 3.10+ stdlib tools.
  - `discover_repo.py` inventories the stack and validation evidence, uses bounded regular-file reads, and refuses symlinks. Config fields can contain sensitive literals; keep raw evidence local.
  - `collect_gh.py` and `analyze_gh.py` pull read-only GitHub Actions data and produce p50/p95 baselines.
  - `benchmark.py` runs paired timing.
  - `select_affected.py` is a fail-closed affected-test selector.
  - [Measured adapters](references/13-measured-adapters.md) add exact-revision
    GitHub check observations, artifact provenance verification, GitLab pipeline
    snapshots, cache inventory/economics, test shard planning, selector shadow
    comparison, and finite-capacity runner estimates. These are separate evidence
    types, not automatic release decisions.
- [evals/](evals/README.md): independent behavior cases and grading protocol;
  included for maintainers, not evidence of executed model evaluations.

## Install

The skill directory name must be `agent-ci-optimizer`. Check each target first with
`ls -ld <target>`. If an old entry exists, rename or remove it, then link with `ln -sn`:

```bash
SKILL_DIR="$PWD"   # this clone
ln -sn "$SKILL_DIR" ~/.agents/skills/agent-ci-optimizer
ln -sn ../../.agents/skills/agent-ci-optimizer ~/.claude/skills/agent-ci-optimizer
ln -sn ~/.agents/skills/agent-ci-optimizer ~/.codex/skills/agent-ci-optimizer
ln -sn ~/.agents/skills/agent-ci-optimizer ~/.pi/agent/skills/agent-ci-optimizer
```

A symlink install serves this working tree as-is, so uncommitted edits reach every agent.
To ship a copy without repo tooling, run `git archive HEAD | tar -x -C <dest>`.

## Test

```bash
python3 -m unittest discover -s tests -v   # scripts, links, anchors, frontmatter
actionlint                                  # workflow lint
```

© 2026 PyModel LLC. MIT [license](LICENSE).
