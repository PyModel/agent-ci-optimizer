# Release automation

Reviewed 2026-11-20 against the current
[Jev Judge release workflow](https://github.com/PyModel/jev-judge-mcp/blob/a4ec04e53b5c97ce873dddb29416768ed5eff4cd/.github/workflows/release.yml)
and its [maintainer-approved CI decision](https://github.com/PyModel/jev-judge-mcp/blob/a4ec04e53b5c97ce873dddb29416768ed5eff4cd/docs/adr/0060-release-pr-ci-is-maintainer-approved.md).

## Activation and normal operation

1. Enable **Settings → Actions → General → Workflow permissions → Allow GitHub
   Actions to create and approve pull requests** if repository/organization policy
   currently prevents bot PR creation. This setting was confirmed enabled for
   this repository on 2026-11-20; no setting change was needed. The workflow never
   approves or merges PRs.
2. Merge this setup through the existing required `ci` check and full merge queue.
   Release Please runs after successful first-attempt `main` push CI. It uses the
   built-in `GITHUB_TOKEN` and creates or updates one version/changelog PR.
3. Review that PR. GitHub creates approval-required PR workflow runs for changes
   made by the built-in token; a maintainer with write access selects **Approve
   workflows to run** in the PR. Approve each new bot revision when requested.
4. Wait for the full required checks and use the existing merge queue. There is
   no release-specific bypass, automatic approval or automatic merge.
5. Once the release PR reaches main and its full CI passes, Release Please creates
   the version tag and a **draft** GitHub release. The read-only verification job
   exports the complete skill at that exact commit and runs its standalone suite.
   The trusted publisher validates the draft by its exact Release Please ID and
   publishes only after every gate succeeds.

There is no separate account, PAT, GitHub App secret, registry or environment
approval to provision. GitHub-hosted standard compute for this public repository
has no additional runner charge; this setup uploads no Actions artifacts. The
new release path is outside PR feedback. Its live latency and monthly usage must
be measured after activation; local test timings are not a production benchmark.

## Versions and notes

The original [v0.1.0 release](https://github.com/PyModel/ci-optimiser/releases/tag/v0.1.0)
and tag point to `d849a540787e37a004a699da0b300b796efd184c` and are preserved.
The starting manifest and `version.txt` both contain `0.1.0`. `bootstrap-sha`
excludes that commit and earlier history from the first generated changelog.
Release Please ignores the bootstrap boundary once it has a merged release PR.

This is a `simple` release strategy: the bot updates `version.txt`,
`.release-please-manifest.json`, and `CHANGELOG.md`. Releases use stable `vX.Y.Z`
tags without a package-name prefix. The installable skill remains `ci-optimiser`
under `skills/ci-optimiser/`; release plumbing stays under `.github/` and is not
part of the installed skill. GitHub source archives and the tagged repository are
the distribution, matching the existing 0.1.0 release; there is no PyPI package.

Use Conventional Commit squash titles for changes that need release notes:

| Title | Normal version effect |
| --- | --- |
| `fix: reject an incomplete job receipt` | Patch |
| `feat: add another CI provider` | Minor |
| `feat!: change the report schema` with an explained breaking change | Major |
| `docs: clarify an existing instruction` or `chore: maintain infrastructure` | Usually no release by itself |

Review the proposed version and changelog before merging. Previous descriptive
commit titles are not automatically classified or rewritten. If the first release
notes need to describe those changes, add a reviewed, accurate summary to the
release PR; do not invent a history or claim that the bot inferred it.

## What the gates establish

The workflow starts only from a successful `ci` completion for a `push` to `main`
in `PyModel/ci-optimiser`. A separate read-only gate then verifies all of:

- Numeric repository identity `1442466406`, repository names, active workflow ID,
  exact `.github/workflows/ci.yml` path and workflow name.
- The event receipt and current API record agree on run ID, exact head SHA,
  branch, event and a completed successful **first** attempt.
- The exact complete job inventory: `test (3.10)`, `test (3.x)`, `workflow-lint`
  and required aggregate `ci`. Missing, duplicate, extra or truncated jobs fail.
- Every required step exists, and every observed step and job completed
  successfully. A skipped step or a green aggregate hiding a red leg fails.
- Current `main` still points to the verified SHA.

Release Please receives write permissions only in its separate version job.
Before the action, a second preflight uses the pinned upstream's actual candidate
selection boundary: the first 200 merged main PRs, ordered by updated time, with
the default `autorelease: pending` label. It counts merged PRs, not closed rows;
unknown, duplicate or incomplete pagination fails. Every pending candidate must
have the exact current-main merge SHA. This prevents an older pending release PR
from being tagged merely because a newer main commit passed CI. The preflight is
conservative about malformed PR bodies and rejects configuration changes that
would alter its candidate scope. Update that policy deliberately when changing
the upstream release strategy or label conventions.

That job also checks main immediately before and after the action. Package verification
has only `contents: read` and `actions: read`, checks Release Please's source SHA
equals the triggering CI SHA, and tests an export containing only the installed
skill. It checks the version file, manifest, changelog and resolved tag commit.

The publisher has `contents: write` and `actions: read`. It runs only the small
stdlib publisher from the already verified main commit, rechecks complete CI,
source/version/tag identity and current main, then reads the exact draft release
ID from Release Please. Draft metadata requires push access, so the read-only
test job never depends on draft visibility. The publisher rejects changed IDs,
unexpected assets, prereleases, already-published releases or mismatched source.
Its only supported write is changing that verified release's `draft` field to
`false`. It checks the resulting metadata, tag and main again after publication.

No privileged job executes PR artifacts or installs/tests the skill. Checkouts
use an explicit SHA, blobless full history and `persist-credentials: false`.
The workflow has empty default permissions and pins each action to a full commit.

These checks do not independently prove branch/ruleset configuration: the
existing full-suite merge queue and required `ci` protection must remain enabled.
There is also no GitHub API transaction that atomically compares main, checks a
tag, freezes pending-PR metadata and publishes a release. Pre/post reads detect
observed races; they cannot eliminate a concurrent writer changing main, the tag,
PR labels or draft metadata between calls. A concurrent tag writer could change
the tag just before publication and be detected only afterward. The guarantee is
the identities actually verified before and after the write, not an atomic lock
on GitHub state. A post-write mismatch leaves the workflow red and requires
investigation; it does not claim an automatic rollback.

## Failures and bounds

The repository publisher makes one bounded API call per operation with no retry
loop. CI and release attempts greater than 1 are rejected. There are no workflow
rerun/republish inputs, ignored failures, skipped suite legs or timeout increases.
A failure leaves the evidence red; investigate and fix it in a new reviewed
commit. Do not rerun a failing check until it happens to pass.

The pinned Release Please dependency itself has upstream GraphQL transport retries
for null responses and HTTP 502, with up to five retries and no action input to
disable them. This is not a retry of CI or tests, but the automation must not be
described as transport-retry-free. The one-minute action bound still applies.
The local verifier/publisher has no retry loop; every failed validation remains
red, and this setup adds no retry to make a check pass.

If main advances while an older CI completion waits, the stale event fails before
mutation. The newer main commit's own first-attempt CI completion can process the
current state. An older merged PR still labelled `autorelease: pending` deliberately
blocks the bot before it creates tags. A maintainer must inspect that pending
release, reconcile its label/state, and prepare a newly reviewed version PR; the
workflow does not silently discard it or publish its older commit. If a draft/tag
has already been created and a later gate fails,
the draft remains unpublished for diagnosis. Fix the cause through a normal PR
and let the next release version proceed; there is no automatic republish or tag
replacement. If a post-publication check fails, investigate the actual public
state before deciding on any corrective release.
A timed-out publication request is reported as an uncertain write, and a failed
post-publication check is reported as a post-write failure; neither is described
as proof that publication was blocked.

| Step | Bound |
| --- | --- |
| Complete source/CI gate | 20 seconds |
| Candidate/source preflight immediately before Release Please | 20 seconds |
| Release Please action | 1 minute |
| Standalone skill tests | Existing strict 60-second test budget |
| Source/version verification | 30 seconds |
| Publication and postchecks | 30 seconds |

Job timeout limits are 1 minute for the source gate/publisher and 2 minutes for
version/package verification. These release-operation bounds are not claimed PR
p50/p95 results. The required PR/queue CI matrix and all its existing budgets stay
the same; its test command includes the repository-only release policy tests.

Run both repository and bundle tests with `python3 .github/run_repository_tests.py`
and lint every workflow with `actionlint`. The offline release tests mutate source
identities, retries, job inventories, steps, versions, drafts and tag targets and
assert that invalid evidence cannot reach the publication write. They do not
substitute for observing the first live bot PR and release.

## Primary sources checked 2026-11-20

- [Release Please v5.0.0 action and inputs](https://github.com/googleapis/release-please-action/tree/45996ed1f6d02564a971a2fa1b5860e934307cf7).
- [Release Please v17.6.0 manifest, bootstrap and draft behavior](https://github.com/googleapis/release-please/blob/v17.6.0/docs/manifest-releaser.md).
- [Simple strategy and version.txt](https://github.com/googleapis/release-please/blob/v17.6.0/src/strategies/simple.ts).
- [Pending release candidate selection](https://github.com/googleapis/release-please/blob/v17.6.0/src/manifest.ts).
- [Upstream API pagination and transport retry behavior](https://github.com/googleapis/release-please/blob/v17.6.0/src/github-api.ts).
- [Built-in-token workflow and PR approval behavior](https://docs.github.com/en/actions/concepts/security/github_token).
- [Repository Actions permissions setting](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository).
- [Release API and draft visibility](https://docs.github.com/en/rest/releases/releases).
- [GitHub-hosted public repository compute](https://docs.github.com/en/billing/concepts/product-billing/github-actions).
