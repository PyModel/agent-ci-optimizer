# Repository guidance

This repo is one agent skill, `ci-optimiser`, packaged at
[skills/ci-optimiser/SKILL.md](skills/ci-optimiser/SKILL.md), with one-hop
[references](skills/ci-optimiser/references/), [templates](skills/ci-optimiser/templates/),
stdlib [scripts](skills/ci-optimiser/scripts/) and their [tests](skills/ci-optimiser/tests/).

- `SKILL.md` stays ≤200 lines and ≤13 KiB; frontmatter is `name: ci-optimiser` and a folded
  `description: >-` starting `Use when `. Detail goes in `references/`.
- Scripts: Python 3.10+ standard library only, no shell=True, read-only against providers,
  missing data reported as `unknown`, never 0. Every behavior change gets a test in `tests/`.
- Tool versions, flags and prices in `references/` cite a dated URL in
  [06-official-sources.md](skills/ci-optimiser/references/06-official-sources.md); unverified claims go in its
  "Not confirmed" list.
- Before committing repository work: run `python3 .github/run_repository_tests.py`
  from the root. It runs the complete bundled suite (including source links/anchors)
  and repository-only release policy tests. Then run `actionlint` on every file in
  `.github/workflows/`. For a standalone installed skill, keep using
  `python3 -m unittest discover -s tests -v` from its `ci-optimiser` directory;
  it does not depend on repository release plumbing.
- Enable the executable pre-push hook using the preservation-aware procedure in
  [README.md](README.md#local-pre-push-hook); its nine-second check supplements full CI.
- Keep one installable entrypoint under `skills/ci-optimiser`; do not add a root `SKILL.md`.
  The bundle must work without checkout-only README, CI or hook files and includes its MIT license.
- Manual clone installs can symlink `skills/ci-optimiser`: uncommitted edits reach every
  linked agent. Skills CLI installs use a managed copy; keep them separate from source checkouts.
