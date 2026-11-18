# Platform compatibility and workflow semantics

Reviewed **2026-11-17**. This is a decision procedure, not permission to enable every
feature. Versions, cloud plans and GitHub Enterprise Server support differ. Recheck
the dated [source register](06-official-sources.md) and the machine-readable
[feature matrix](feature-matrix.json) for the target installation.

## Contents

- [Semantic inspection](#semantic-inspection)
- [Concurrent steps](#concurrent-steps)
- [Cache access and economics](#cache-access-and-economics)
- [Action dependency identity](#action-dependency-identity)
- [Scheduling and approval](#scheduling-and-approval)
- [Retention and collection](#retention-and-collection)
- [Runner selection](#runner-selection)
- [Maintenance and verification](#maintenance-and-verification)

## Semantic inspection

The discovery scanner deliberately returns hints. A line matching `uses:` cannot
prove an action's immutable resolution; a job name cannot prove test coverage.
For deeper inspection, convert approved workflow files using a **pinned YAML 1.2
parser** with duplicate-key rejection, preserving mappings, arrays and nulls. Feed
the resulting JSON to `scripts/inspect_workflows.py`. Read its `--help` first.
The Python core does not install a parser or execute a repository's configuration.

Retain the original path and file hash, parser/version, provider/version, event,
caller context and source commit with the evidence. Reject YAML 1.1's conversion
of `on` to a boolean. JSON conversion is a trust boundary: parser errors, unsupported
tags, unknown aliases or lossy conversion cannot establish a passing policy.

Resolve matrices, expressions, reusable workflows and composite actions from the
exact source revision before calling a graph complete. Unresolved expansion stays
`unknown`; inspect the platform's validated workflow/run graph. A structural
inspector is not the platform's workflow compiler and cannot certify runtime behavior.

## Concurrent steps

GitHub added native `parallel`, `background`, `wait`, `wait-all` and `cancel` steps
in June 2026. Prefer those primitives when the target supports them and independent
work benefits from sharing setup. Keep separate jobs for distinct trust/resource
boundaries. Sources: [announcement](https://github.blog/changelog/2026-06-25-actions-steps-can-now-be-run-in-parallel/)
and [workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax).

Record synchronization edges as well as job `needs`. Background outputs become
usable after synchronization; failures must propagate. There are at most ten
concurrent background steps per job. A composite action may be launched in the
background, but cannot declare background or parallel steps internally. `wait`,
`wait-all` and `cancel` always run and do not support `if`. Inspect implicit final
waiting, service shutdown and cleanup; a server that never terminates can stall CI.

**Experiment:** measure CPU/memory, shared directory writes, duration and overlap
before/after. **Gate:** failure/cancellation reaches the required aggregate; outputs
cannot race; cleanup finishes; total work and interval-union elapsed time are
reported separately. **Fallback:** supported sequential steps or independent jobs.
Do not substitute summed step seconds for elapsed job time.

## Cache access and economics

Workflow/job `cache-mode` is a token capability, distinct from Docker's exporter
`mode=min|max`. The job setting overrides the workflow setting. The four access
sets are:

| Mode | Restore | Save |
|---|---|---|
| `read` | yes | no |
| `write` | yes | yes |
| `write-only` | no | yes |
| `none` | no | no |

For a reusable workflow, an **explicit** caller limit bounds the callee. Requesting
more access is a validation error, including incompatible read/write-only requests.
A low-trust default alone is not an explicit caller cap. Ordinary `pull_request`
caches remain merge-ref scoped; do not label every PR cache read-only. Explicit
write access can override low-trust defaults and requires a reviewed trust argument.
Source: [cache access reference](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching).

Measure restored/saved bytes, restore/save seconds, hit rate, eviction frequency,
quota and net avoided work. GitHub's default repository cache allowance is 10 GB;
larger configured limits can incur storage charges. Report the actual plan/date and
approved limit, not a universal free-cache assumption. Source:
[Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

**Adopt only if** cold builds remain correct, trusted readers cannot consume
untrusted executable outputs, invalidation covers all inputs, and restore/save
overhead plus misses is a measured net benefit. Exercise cache outage and corruption
fallback. Reducing retention is not a substitute for fixing high-cardinality keys.

## Action dependency identity

Recognize full commit SHAs, Docker digests, native lockfile resolution, checkout
relative `./` actions, `$/` actions from the same repository at the running commit,
and unresolved mutable references separately. A checkout action can change what `./` executes.
`$/` resolves against the repository and running commit of the workflow or action
containing it, including inside a called workflow or action. It is unavailable on
GHES. Review migrations against the [workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax).

Native `.github/workflows/actions.lock` is generated by `gh actions-lock`, with
format/version and per-workflow coverage validated. The current documented format
is `v0.0.3`; it records the action ref, commit and repository/owner IDs. Never
hand-edit generated resolution. The platform verifies reachability and identity;
offline format checks do not establish either. The reviewed how-to lists **GitHub
Enterprise Cloud** availability; verify support before adopting it on another plan
or installation.

Job-level reusable workflow calls need their own immutable reference. Their action
dependencies are governed by the source repository's lockfile, not the caller's.
Do not count a mutable reusable call as pinned merely because the caller has a lock.
Sources: [concept](https://docs.github.com/en/actions/concepts/security/workflow-dependency-lockfiles)
and [generation/review](https://docs.github.com/en/actions/how-tos/secure-your-work/lock-workflow-dependencies).

**Migration gate:** review every resolved commit/identity change, allowlist effects,
local/composite dependencies and transitive coverage. Keep direct SHAs where native
support is unverified. A lockfile pins reviewed code; it does not prove code is safe.

## Scheduling and approval

- **Concurrency queue:** `queue: max` allows a bounded queue of up to 100 pending
  runs/jobs in a group. It is incompatible with `cancel-in-progress: true`. It is
  separate from a merge queue, and cancellation/ordering semantics must be tested.
  A rolling full-suite lane must guarantee completion and bound green age/head lag.
  Source: [concurrency controls](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).
- **Bot PR approval:** `pull_request` events with `opened`, `synchronize` or
  `reopened` activity generated by `GITHUB_TOKEN` create approval-required runs.
  Classify this as approval wait, not runner queue; keep draft/ready transitions
  and bot identity in the cohort. Source:
  [automatic token authentication](https://docs.github.com/en/actions/concepts/security/github_token).
- **Public privileged trigger change:** GitHub announced default blocking of
  `pull_request_target` for public repositories beginning **2026-12-02**. Private
  and internal repositories, and GHES, have different applicability. Inspect actual
  settings; do not convert to privileged execution or enable a bypass to gain speed.
  Source: [announcement](https://github.blog/changelog/2026-11-02-pull_request_target-blocked-by-default-december-2/).

## Retention and collection

Since October 2026 on **github.com**, Actions retention also covers checks, runs
and statuses, including third-party checks. Verify GHES behavior separately.
Deleted evidence cannot be reconstructed from a green badge. Collect bounded,
immutable snapshots with a manifest; retain only the policy-approved evidence
needed for comparisons and releases. Source:
[retention update](https://github.blog/changelog/2026-10-01-actions-retention-now-covers-checks-runs-and-statuses/).

Collector errors distinguish incomplete enumeration, missing jobs, inaccessible
attempts, policy permissions and pagination limits. Authentication-related 404 is
not proof that protection is absent. Server-side date/workflow filters reduce API
work, but query caps and retention still limit recall. ETag reuse means revalidated
payload, not freshly rerun work. See [evidence formats](11-evidence-formats.md).

## Runner selection

Treat provider, hardware, architecture, OS/container, trust, toolchain and cache as
separate dimensions. For `ubuntu-slim`, verify the current one-CPU container model,
15-minute job bound and missing capabilities before considering short lightweight
jobs. Standard VM assumptions are not portable. ARM options require dependency and
image compatibility, not just a new label. Sources:
[hosted runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
and [self-hosted runners](https://docs.github.com/en/actions/concepts/runners/self-hosted-runners).

Eligible standard and compatible larger 2-core/4-core Linux hosted jobs may use
Azure or AWS. The reviewed rollout is automatic for Free/Pro/Team; Enterprise
Cloud can opt in. Windows and specialized configurations such as private networking
are excluded. Record effective network/runner context and keep egress allowlists
aligned with GitHub's published Actions ranges. Source:
[multi-cloud announcement](https://github.blog/changelog/2026-10-28-github-actions-adds-multi-cloud-compute-to-improve-reliability/).

## Maintenance and verification

Pin workflow dependencies under the repository's policy and automate reviewed
update proposals. Keep an actionlint gate for supported syntax plus semantic
fixtures for new features the installed linter may not yet understand. Do not treat
an unsupported keyword as proof the platform feature is unsafe or available.
Execute negative aggregate/report tests, inspect the actual CI run at the changed
commit, and record which provider-specific behaviors were only source-reviewed.
