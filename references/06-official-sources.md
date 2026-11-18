# Official sources

Initial source review: 2026-11-01. Expanded platform, measurement and stack review:
2026-11-17. Dates below state the review that supports a claim, not a guarantee of
future compatibility. Re-open sources before quoting a current version or price.

| Topic | Fact relied on | URL |
|---|---|---|
| Bun type checker | `bun check` added in 1.4.3; `bun check --no-script` always runs the checker and accepts its flags; needs `@types/bun`; follows TS 7.1 since 1.4.4 | https://bun.com/docs/runtime/check · https://bun.com/blog/bun-v1.4.4 · https://bun.com/blog/bun-v1.4.3 |
| TypeScript 7 | 7.0 stable 2026-07-08 as `tsc` in `typescript`; no compiler API until 7.1; `@typescript/typescript6` gives `tsc6`; deprecated 6.0 options are errors; `--build` works | https://devblogs.microsoft.com/typescript/announcing-typescript-7-0/ |
| Oxlint | ~870 rules; plugin coverage; type-aware linting; JS plugins alpha; `@oxlint/migrate`; `eslint-plugin-oxlint` | https://oxc.rs/docs/guide/usage/linter |
| Vitest 5 | `isolate` default true; `pool` default `forks`; `maxWorkers`; `--shard` by file with blob reporter; `sequence.shuffle` | https://vitest.dev/config/isolate · https://vitest.dev/config/pool · https://vitest.dev/config/maxworkers · https://vitest.dev/guide/cli |
| Vitest 4 migration | `poolOptions` removed; `maxThreads`/`maxForks` → `maxWorkers` | https://v4.vitest.dev/guide/migration |
| actions/checkout v7 | `fetch-depth` default 1; `filter` input (partial clone, overrides sparse-checkout); `allow-unsafe-pr-checkout`; `submodules` (default false) and `lfs` inputs (re-checked 2026-11-02) | https://github.com/actions/checkout |
| Required checks | skipped workflow → Pending, blocks merge; skipped job → Success | https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks · https://docs.github.com/en/pull-requests/reference/status-checks |
| `pull_request_target` | base-repo token and secrets; pwn-request risk | https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target |
| Dependency caching | branch/default-branch restore scope; PR caches scoped to merge ref | https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching |
| setup-node / setup-python caching | npm auto-cache only with `packageManager`; python opt-in | https://github.com/actions/setup-node · https://github.com/actions/setup-python |
| Nx affected | default base main; `NX_BASE`/`NX_HEAD`; `nx-set-shas` | https://nx.dev/docs/features/ci-features/affected |
| Turborepo affected | `--affected` = `--filter=...[main...HEAD]`; `TURBO_SCM_BASE`; shallow checkout ⇒ everything changed | https://turborepo.dev/docs/reference/run |
| Blacksmith | label format; free tier minutes; marketing speed claim; per-job Firecracker microVM, destroyed after the job | https://docs.blacksmith.sh/blacksmith-runners/overview · https://www.blacksmith.sh/security |
| Depot | label; ephemeral single-tenant EC2; per-second billing | https://depot.dev/docs/github-actions/overview |
| Larger runners | Team / Enterprise Cloud only; per-minute billing | https://docs.github.com/en/actions/concepts/runners/larger-runners |
| Runner pricing | per-minute rates by label | https://docs.github.com/en/billing/reference/actions-runner-pricing |
| Merge queue | workflows need `merge_group` trigger | https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue |
| PR activity types (accessed 2026-11-02) | `pull_request` runs by default on `opened`, `synchronize`, `reopened`; `edited`, `labeled`, `assigned`, `review_requested` etc. are opt-in types | https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows |
| Matrix strategy (accessed 2026-11-02) | `strategy.fail-fast` defaults to true and cancels in-progress and queued matrix jobs when one fails; without `max-parallel`, GitHub runs as many matrix jobs in parallel as runners allow | https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax |
| OIDC permission (accessed 2026-11-02) | `id-token: write` is required to request the JWT and can be set on a single job | https://docs.github.com/en/actions/reference/security/oidc |

## Validation architecture and reviewed updates (2026-11-17)

The four-category placement, 300-second PR objective, rolling-main controls and
release evidence contract are proposed design rules, not measured improvements or
provider guarantees. Keep exact repository policy and tool/platform support explicit.

| Topic | Fact relied on | URL |
|---|---|---|
| Merge candidates | Required queue checks apply to the combined candidate; GitHub Actions needs `merge_group`; temporary candidate SHA differs from the PR | https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue |
| Artifact attestations | Build source/workflow provenance must be verified; provenance alone does not establish safety or passing tests | https://docs.github.com/en/actions/concepts/security/artifact-attestations |
| Deployment gates | Jobs must satisfy configured environment protection before execution/access to environment secrets; availability depends on repository/plan | https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments |
| Progressive delivery | Optional Argo example: analysis results can continue, abort or pause a rollout; use the existing platform's equivalent | https://argo-rollouts.readthedocs.io/en/stable/features/analysis/ |
| Draft lifecycle | Default PR types exclude `ready_for_review`; add it when draft skipping relies on a later ready transition | https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows |
| Matrix cancellation | `fail-fast` controls sibling cancellation; `continue-on-error` tolerates a failure | https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax |
| Vitest 5 reports | Default blob reports moved to `.vitest/blob/blob-*.json`; explicit output paths remain supported | https://vitest.dev/guide/migration/ · https://vitest.dev/guide/reporters |
| Missing artifacts | upload-artifact defaults to warning for no matching files; `if-no-files-found: error` makes missing required reports fail | https://github.com/actions/upload-artifact |
| Workflow lockfiles | `.github/workflows/actions.lock` resolves action dependencies; reusable workflow calls require separate pinning; discovery markers alone do not verify resolution | https://docs.github.com/en/actions/concepts/security/workflow-dependency-lockfiles |
| Parallel steps | Native `background`, `parallel`, wait and cancel syntax; measurements must account for overlapping step intervals | https://github.blog/changelog/2026-06-25-actions-steps-can-now-be-run-in-parallel/ |
| Cache access | Workflow/job `cache-mode` is service-enforced; inspect effective scope rather than inferring permission from action names | https://github.blog/changelog/2026-09-10-control-github-actions-cache-access-with-cache-mode/ |

## Measurement and implementation sources (2026-11-17)

| Topic | Fact relied on | URL |
|---|---|---|
| Workflow runs and attempts | Date/workflow filters, run-attempt endpoints and query limits; distinguish a rerun from a different run | https://docs.github.com/en/rest/actions/workflow-runs |
| Workflow jobs | Paginated jobs and attempt identifiers; timing evidence is not a complete workflow DAG | https://docs.github.com/en/rest/actions/workflow-jobs |
| Branch rules | Paginate applicable rules; preserve required-check app and required-workflow identities | https://docs.github.com/en/rest/repos/rules |
| REST best practice | Serial requests, bounded retry/backoff and conditional requests | https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api |
| REST authentication errors | An inaccessible private resource can return 404; absence must not be inferred | https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api |
| REST API version | Collector explicitly requests 2026-03-10; record the actual API version | https://docs.github.com/en/rest/about-the-rest-api/api-versions |
| Subprocess lifecycle | POSIX session/process-group controls and timeout handling; process launch is not containment | https://docs.python.org/3/library/subprocess.html · https://docs.python.org/3/library/os.html#os.killpg |
| actionlint | Own CI pins v1.7.12 and verifies release archive SHA-256; new platform syntax needs separate compatibility checks | https://github.com/rhysd/actionlint · https://github.com/rhysd/actionlint/releases/tag/v1.7.12 |
| Action update proposals | Dependabot github-actions ecosystem and schedule configuration | https://docs.github.com/en/code-security/dependabot/dependabot-version-updates/configuration-options-for-the-dependabot.yml-file |

## Platform updates (2026-11-17)

| Topic | Fact relied on | URL |
|---|---|---|
| Cache capabilities | Job override; read/write/write-only/none; explicit reusable caller limit; ordinary PR merge-ref scope | https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching |
| Native workflow structure | Parallel/background/wait/cancel syntax, composite limitations and same-repository action resolution | https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax |
| Lockfile generation | Enterprise Cloud support documented; generated lockfiles, local-action migration and reusable source-repository coverage | https://docs.github.com/en/actions/how-tos/secure-your-work/lock-workflow-dependencies |
| Lockfile format | v0.0.3; per-workflow transitive pin coverage, owner/repo identities and reference normalization | https://github.com/github/actions-lockfile |
| Concurrency queue | queue:max bounded at 100; cannot combine with cancel-in-progress:true; distinct from merge queue | https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency |
| Public privileged trigger | Announced default block for public pull_request_target from 2026-12-02; scope exceptions matter | https://github.blog/changelog/2026-11-02-pull_request_target-blocked-by-default-december-2/ |
| Retention | Actions retention now includes checks/runs/statuses on github.com; deleted evidence unavailable | https://github.blog/changelog/2026-10-01-actions-retention-now-covers-checks-runs-and-statuses/ |
| Bot-created PRs | Selected GITHUB_TOKEN PR events create approval-required runs; approval is not runner queue | https://docs.github.com/en/actions/concepts/security/github_token |
| Hosted runner constraints | Slim/container resources and timeout, architecture and environment distinctions | https://docs.github.com/en/actions/reference/runners/github-hosted-runners |
| Self-hosted runner support | Supported systems/architectures and responsibility for environment/isolation | https://docs.github.com/en/actions/concepts/runners/self-hosted-runners |
| Multi-cloud compute | Eligible hosted Linux workloads, plan-specific adoption and expanded egress ranges | https://github.blog/changelog/2026-10-28-github-actions-adds-multi-cloud-compute-to-improve-reliability/ |
| Cache economics | 10 GB included per repository; higher configured limits can incur storage charges | https://docs.github.com/en/billing/concepts/product-billing/github-actions |

## Stack adapter sources (2026-11-17)

| Topic | Fact relied on | URL |
|---|---|---|
| Go | Test-result caching, -count, package/test parallelism and build cache semantics | https://pkg.go.dev/cmd/go |
| setup-go | v7 cache behavior, go.mod input and current action requirements | https://github.com/actions/setup-go |
| Next.js cache | .next/cache persistence for CI | https://nextjs.org/docs/app/guides/ci-build-caching |
| Next.js type generation | next typegen and distinct production build behavior | https://nextjs.org/docs/app/api-reference/cli/next |
| Vitest filesystem module cache | Current default and cache/plugin compatibility controls | https://vitest.dev/config/fsmodulecache |
| Vitest shared server | Compatible inline-project reuse and configuration constraints | https://vitest.dev/config/sharedviteserver |
| Vitest profiling | Separate transform/setup/execution bottlenecks before reducing isolation | https://vitest.dev/guide/profiling-test-performance · https://vitest.dev/guide/improving-performance |
| Vitest coverage | Provider/config/threshold obligations remain when sharding | https://vitest.dev/guide/coverage.html |
| Combined typed lint | oxlint --type-aware --type-check reports compiler diagnostics; compatibility and declaration gates remain | https://oxc.rs/docs/guide/usage/linter/type-aware.html |
| TypeScript release distinction | 7.1 beta is separate from the stable 7.0 migration and has its own compatibility scope | https://devblogs.microsoft.com/typescript/announcing-typescript-7-1-beta/ |
| Formatting | Oxfmt coverage and Bun 1.4.4 formatter/check/no-script behavior; vendor timing is not repository evidence | https://oxc.rs/docs/guide/usage/formatter · https://bun.com/blog/bun-v1.4.4 |
| Docker GHA cache | Image scopes, mode=min/max, v2 compatibility and cache mount caveats | https://docs.docker.com/build/cache/backends/gha/ · https://docs.docker.com/build/ci/github-actions/cache/ |
| Playwright | File/test distribution, fullyParallel and report merging | https://playwright.dev/docs/test-sharding |
| Nx remote cache | Task determinism, remote-cache behavior and trust requirements | https://nx.dev/docs/features/ci-features/remote-cache |
| Turborepo remote cache | Cached task outputs/logs and integrity/signature options | https://turborepo.dev/docs/core-concepts/remote-caching |

## Not confirmed for an arbitrary target repository

Current paid-runner quotes; actual billing/allowances; GHES/plan feature support;
downstream framework/compiler/plugin compatibility; complete test-selection coverage;
cryptographic provenance verification; telemetry comparability; measured savings.
These require target evidence. Python helper fixtures do not establish production
support for every stack or a measured performance improvement.
