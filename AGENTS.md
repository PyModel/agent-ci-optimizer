# Repository guidance

This repo is one agent skill, `agent-ci-optimizer`: [SKILL.md](SKILL.md), one-hop
[references](references/), [templates](templates/), stdlib [scripts](scripts/) and their [tests](tests/).

- `SKILL.md` stays ≤200 lines; frontmatter is `name: agent-ci-optimizer` and a folded
  `description: >-` starting `Use when `. Detail goes in `references/`.
- Scripts: Python 3.10+ standard library only, no shell=True, read-only against providers,
  missing data reported as `unknown`, never 0. Every behavior change gets a test in `tests/`.
- Tool versions, flags and prices in `references/` cite a dated URL in
  [06-official-sources.md](references/06-official-sources.md); unverified claims go in its
  "Not confirmed" list.
- Before committing: `python3 -m unittest discover -s tests -v` (also checks links/anchors)
  and `actionlint` on `.github/workflows/`.
- The install is a symlink to this working tree: uncommitted edits reach every agent at once.
