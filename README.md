# agent-ci-optimizer

An agent skill that makes CI faster without making it lie. It audits a repo's
push → trustworthy green → merge loop, measures where the time goes, and proposes one
reversible, benchmarked change at a time across seven modules: fast runners, native
lint/typecheck, bigger machines and test-worker reuse, caching and blobless clones,
parallel jobs, affected tasks, and pre-push hooks. It starts read-only and treats a
false green as worse than a slow build.

## Contents

- [SKILL.md](SKILL.md): the procedure agents follow.
- [references/](references/): discovery matrix, playbook, safety rules, measurement, examples, dated sources, config recipes.
- [templates/](templates/): audit report, optimization PR, paste-ready brief for agents without the skill.
- [scripts/](scripts/): Python 3.10+ stdlib tools.
  - `discover_repo.py` inventories the stack and never opens secret files.
  - `collect_gh.py` and `analyze_gh.py` pull read-only GitHub Actions data and produce p50/p95 baselines.
  - `benchmark.py` runs paired timing.
  - `select_affected.py` is a fail-closed affected-test selector.

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
