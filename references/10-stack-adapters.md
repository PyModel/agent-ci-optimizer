# Stack-specific optimization adapters

Reviewed **2026-11-17**. Activate only on positive repository evidence. These are
candidate experiments, not migrations applied automatically. Use locked installed
versions and the repository's package-manager execution convention; the command
examples name tools, not permission to download an unpinned executable.

Every adapter must record detection evidence, workload identity, baseline, one
intervention, cold/warm results, correctness gates, total runner work and rollback.
Unknown support means retain the existing command. See the
[source register](06-official-sources.md) and [feature matrix](feature-matrix.json).

## Contents

- [Go](#go)
- [Next.js](#nextjs)
- [Vitest transform and worker reuse](#vitest-transform-and-worker-reuse)
- [Vitest shard and coverage evidence](#vitest-shard-and-coverage-evidence)
- [Typed lint and TypeScript](#typed-lint-and-typescript)
- [Formatting](#formatting)
- [Docker and BuildKit](#docker-and-buildkit)
- [Playwright](#playwright)
- [Nx and Turborepo remote caches](#nx-and-turborepo-remote-caches)
- [Artifact transport](#artifact-transport)
- [Runner resources](#runner-resources)
- [Android and Gradle](#android-and-gradle)
- [iOS and Xcode](#ios-and-xcode)
- [Python and Rust](#python-and-rust)
- [Toolchain maintenance](#toolchain-maintenance)

## Go

**Detect:** `go.mod`/`go.work`, actual `go test`/build commands, toolchain version,
CGO/race/coverage requirements, generated files and external test services. Separate
module download, compilation and cached test results before calling tests slow.

**Experiment:** compare unchanged workloads with `go test ./...`; use
`go test -count=1 ./...` when measuring actual test execution rather than cached
results. Measure `-p`, test `-parallel` and `GOMAXPROCS` independently against CPU,
memory and shared services; none is a universal fastest setting.

**Gate:** preserve required `-race`, coverage, build tags, environment and package
list; invalidate for toolchain/platform/CGO inputs; test a truly cold cache and one
changed dependency. setup-go's cache is not proof that every run reused test
results. The reviewed v7 action uses the root `go.mod` hash by default; inspect
`cache-dependency-path` for overrides and multi-module paths. **Fallback:** existing
full commands and correct uncached execution. Sources: [Go command](https://pkg.go.dev/cmd/go),
[setup-go](https://github.com/actions/setup-go).

## Next.js

**Detect:** Next dependency/config, router modes, type generation, build env and
`.next` outputs. Distinguish package installation, `.next/cache`, generated types,
application build and browser tests.

**Experiment:** restore only the supported build cache with keys covering lockfile,
Next/toolchain/config and relevant source inputs. If standalone type checking
requires generated route types, preserve `next typegen` before that check. It loads
Next's config in the production build phase, so provide the required environment
and dependencies. It does not replace production-build validation of application
output.

**Gate:** cold build, route/type generation, framework plugins, env-dependent
rendering and the actual production build still pass; never promote a cached
`.next` deployment output without matching provenance. **Fallback:** current full
build. Sources: [CI caching](https://nextjs.org/docs/app/guides/ci-build-caching),
[Next CLI](https://nextjs.org/docs/app/api-reference/cli/next).

## Vitest transform and worker reuse

**Detect:** exact Vitest/Vite version, projects, plugins, test pool, worker count,
transform time, setup files, execution time and fixture lifecycle. Profile first.

**Experiment order:** remove duplicate setup; validate supported filesystem module
cache behavior (`fsModuleCache` defaults to true in Vitest 5 and does not apply to
Browser Mode). The reviewed `sharedViteServer` defaults to true for compatible
inline projects; file/directory projects and projects changing the Vite config
still create separate servers. Then size workers and shards. Only consider
`isolate: false` for suites independently shown to tolerate it.

**Gate:** cache invalidation for plugin/config/environment changes; equal tests and
coverage; multiple shuffled orders and each test file alone; no shared globals,
timers, mocks or resource leaks. A transform cache must not replay stale module
results. Plugin inputs outside tracked source/config may need a cache-key generator
or a module-cache opt-out; plugin options are not universally tracked. Record the
actual resolved project settings. **Fallback:** isolation and existing transform
behavior. Sources:
[filesystem cache](https://vitest.dev/config/fsmodulecache),
[shared server](https://vitest.dev/config/sharedviteserver),
[profiling](https://vitest.dev/guide/profiling-test-performance).

## Vitest shard and coverage evidence

**Detect:** shard count, unique test identities, reporter path, coverage provider,
thresholds and downstream merge command. Vitest 5's default blob location is
`.vitest/blob/blob-*.json`; honor explicit output overrides.

**Experiment:** use `vitest run --shard=i/n --reporter=blob`, preserving the project's
coverage options. Upload a distinct artifact for each shard, fail on missing files,
download artifacts from that exact run/attempt, validate the expected shard set,
then merge with the same installed Vitest/config. Preserve threshold enforcement in
the merge command and fail the aggregate if any shard or merge fails.

Use `check_reports.py` to validate/stage downloaded JSON report transport. It checks
missing/extra shards, empty/malformed/oversized files and symlinks; it does not
validate test success or the meaning of coverage. The test runner remains the
authority. Keep provider/version-specific coverage payloads with each shard;
transporting only a summary or omitting untested files must not weaken thresholds.

**Gate:** deliberately remove one report, fail a shard, lower coverage below its
threshold and cancel a shard. Every case must reject green. Equal report counts
alone are not proof of equal test coverage. **Fallback:** unsharded full suite.
Sources: [migration](https://vitest.dev/guide/migration/),
[reporters](https://vitest.dev/guide/reporters),
[coverage](https://vitest.dev/guide/coverage.html).

## Typed lint and TypeScript

**Detect:** compiler version, declaration/build outputs, framework integrations,
compiler API consumers, typed lint rules and duplicate program construction.

**Experiment:** when installed Oxlint/tsgolint supports it, evaluate combined typed
lint and type checking instead of duplicate program creation. Type-aware lint alone
does not mean compiler diagnostics are enforced. Compare exact diagnostics and
seeded failures before changing gates; retain unsupported ESLint/plugin rules.

For compiler changes, distinguish stable TypeScript 7.0's `tsc` from preview names
and the separate 7.1 beta. Bun's embedded checker version is a separate toolchain
input; `bun check --no-script` avoids package-script dispatch. This is not a blanket
substitute for build mode, declaration emit or every framework's tooling.

**Gate:** error/rule equivalence, include/ignore coverage, project references,
generated declarations and downstream consumers; test both positive and negative
fixtures. **Fallback:** original type/build command plus lint fallback. Sources:
[Oxlint typed lint](https://oxc.rs/docs/guide/usage/linter/type-aware.html),
[TS 7.0](https://devblogs.microsoft.com/typescript/announcing-typescript-7-0/),
[TS 7.1 beta](https://devblogs.microsoft.com/typescript/announcing-typescript-7-1-beta/),
[Bun check](https://bun.com/docs/runtime/check).

## Formatting

**Detect:** authoritative formatter/config/plugins, ignored paths and actual cost.
If formatting is already negligible, keep it. Evaluate Oxfmt or Bun 1.4.4's
`bun fmt --check --no-script` only against supported syntax and the current tool.

**Gate:** identical selected files, ignore behavior, embedded-language support,
check-mode exit status and acceptable formatting diffs. Keep unsupported plugin
formats on the old tool. Never turn a non-mutating CI check into a silent rewrite.
**Fallback:** existing formatter. Sources:
[Oxfmt](https://oxc.rs/docs/guide/usage/formatter),
[Bun 1.4.4](https://bun.com/blog/bun-v1.4.4).

## Docker and BuildKit

**Detect:** Dockerfile stages, context size, base digests, builder/version, target
platforms, cache backend, image scope and secrets passed as proper build secrets.

**Experiment:** profile context transfer and expensive layers; give each image an
appropriate GHA cache scope instead of overwriting the default `buildkit` scope.
Compare exporter `mode=min` and `mode=max` by net cost, not layer count. GHA cache
API v2 minimums in the reviewed guidance include Buildx 0.21.0 and BuildKit 0.20.0;
also check Compose 2.33.1 when using Compose and Engine 28.0.0 when using the Docker
driver with its containerd image store.
Cache mounts are not exported to GHA automatically; measure an explicit solution
before adding another action or persistent builder.

**Gate:** cold/offline-cache correctness, same image/platform outputs, no secret
leakage, trusted writers and cancellation safety. Do not cache credential-bearing
layers. **Fallback:** reproducible uncached build. Sources:
[GHA backend](https://docs.docker.com/build/cache/backends/gha/),
[Actions cache management](https://docs.docker.com/build/ci/github-actions/cache/).

## Playwright

**Detect:** projects/browsers, file sizes, serial suites, setup dependencies,
web-server startup, browser installation, traces/retries and report merge.

**Experiment:** profile setup separately; compare workers × shards. Default
file-oriented sharding and `fullyParallel` distribute work differently; changing
the latter requires tests independent within files. Share a build only when the
serial build/transport stage reduces the required path or justified runner cost.

**Gate:** every expected shard/report and original browser/project set, failure
propagation, retained traces on failure, compatible report merge, no service/data
races, and full coverage/behavioral obligations. **Fallback:** original project
schedule. Source: [Playwright sharding](https://playwright.dev/docs/test-sharding).

## Nx and Turborepo remote caches

**Detect:** a real project/task graph, deterministic tasks, declared inputs/outputs,
environment hashing, cache-hit evidence and an approved cache service. Correct
affected selection and correct task-output caching are separate proofs.

**Experiment:** restore the same task on a clean checkout; compare outputs against
an uncached run. Change a lockfile, toolchain, relevant env/config and transitive
input; all must invalidate. Include restore/upload time and logs/artifacts stored.

**Gate:** trusted writers, project isolation, configured integrity/signature checks
where supported, no secrets in cached logs, and a correct uncached fallback during
outage. Run periodic uncached canaries under the accepted policy. Remote cache hits
are not test execution; report reused validation separately. Nx Cloud's guidance
also recommends bypassing task-cache reuse when building the artifact to deploy.
For Turborepo, artifact signature verification is a configurable capability, not a
default assumption. **Fallback:** local or uncached execution. Sources:
[Nx caching](https://nx.dev/docs/features/ci-features/remote-cache),
[Turborepo caching](https://turborepo.dev/docs/core-concepts/remote-caching).

## Artifact transport

**Detect:** byte sizes, compressibility, upload/download times, retention and every
consumer. For already-compressed payloads, benchmark upload compression level 0;
for text, reduced bytes may win. Include build-once fan-out's serial barrier.

**Gate:** immutable artifact identity, exact run/attempt/source, expected files,
digest verification, same output and fail-on-missing behavior. Scope hidden-file
uploads narrowly; never include an entire workspace to obtain reports. Keep only
the retention required by policy. **Fallback:** existing validated transport.
Source: [upload-artifact](https://github.com/actions/upload-artifact).

## Runner resources

Use measured CPU, memory, I/O, queue, startup, network and cache evidence. Evaluate
slim/ARM/larger/approved external runners only against actual architecture and
capability requirements. Preserve trust, image/toolchain identity and cancellation.
An improvement in execution can lose overall if queue/setup grows. Purchases and
provider migrations need scoped authorization. See [platform guidance](09-platform-updates.md).

## Android and Gradle

**Detect:** wrapper, Gradle/JDK/AGP versions, build variants, unit vs instrumented
tasks, emulator images and the host's actual memory/virtualization capability.
Time configuration, task execution, SDK/AVD provisioning and emulator readiness
separately. A KVM-dependent plan needs verified guest support; ARM alone is not proof.

**Experiment:** independently benchmark dependency downloads, Gradle configuration
cache and task-output build cache. Start with existing tasks and pinned toolchain;
compare cold and warm runs and key any reusable state on actual inputs. Cap Gradle
workers, test forks and emulator count against peak concurrent memory. Include
daemon startup/reuse; do not trade sustained swapping or thermal throttling for more
workers. Keep release signing separate from PR test workers.

**Gate:** same variants, ABI, tests, coverage and instrumented test obligations;
check plugin compatibility and cold fallbacks; fail on missing reports or skipped
emulators. Sources: [Gradle performance](https://docs.gradle.org/current/userguide/performance.html),
[configuration cache](https://docs.gradle.org/current/userguide/configuration_cache.html),
[Android emulator acceleration](https://developer.android.com/studio/run/emulator-acceleration).

## iOS and Xcode

**Detect:** selected Xcode version, macOS runner architecture, schemes and test
plans, simulator runtimes and device destinations. Time boot/provisioning,
dependency resolution, build, test, and report transport separately.

**Experiment:** compare current `xcodebuild test` with `build-for-testing` followed
by `test-without-building` only on compatible toolchains, destinations and files.
Preserve the tested product and expected test inventory through immutable transport.
Measure startup overhead, total runner work, and simulator contention when sharding.
**Gate:** preserve destinations, variants and result bundles; confirm fresh runner
success and isolation of signing/release credentials. Source: [Apple test automation](https://developer.apple.com/library/archive/technotes/tn2339/_index.html).

## Python and Rust

For Python, distinguish interpreter/dependency setup from test execution. Record ABI,
architecture, lockfile and fixture resources. Test parallel scheduling against
isolated databases, ports and temp paths; keep serial groups and compare complete
result inventories. Source: [pytest-xdist distribution](https://pytest-xdist.readthedocs.io/en/stable/distribution.html).

For Rust, profile compilation and link time separately with Cargo timings. Key
compiler/build artifacts to the target, features, toolchain, profile and native linker
inputs, and validate cache-off behavior. Compare clean and incremental workloads
separately; never use cached test results as evidence of a fresh run. Source:
[Cargo timings](https://doc.rust-lang.org/cargo/reference/timings.html).

## Toolchain maintenance

Store exact tested versions and dates in the feature matrix, including action
runtime requirements. A source-reviewed feature is not an executed compatibility
test. This skill's CI tests Python 3.10 and current 3.x, validates its workflows with
pinned actionlint, and runs its behavioral/packaging fixtures. Dependabot proposes
GitHub Action updates for review. Downstream JS/Go/browser/Docker suites must still
be run in the target repository; this Python skill does not claim to have executed
all those stacks.
