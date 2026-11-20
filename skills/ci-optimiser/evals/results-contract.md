# Behavior result contract

Use `ci-behavior-results/1` for an offline, human-graded comparison. This is a record
of declared observations, not authenticated execution or automated semantic grading.
Reject unknown or missing fields. JSON uses finite numbers and unique object keys.

## Frozen inputs and arms

| Top-level field | Required value |
|---|---|
| `schema` | `ci-behavior-results/1` |
| `suite_sha256`, `rubric_sha256` | Canonical JSON SHA-256 of the complete public suite and rubric |
| `baseline_arm`, `candidate_arm` | Distinct blinded labels, for example `A` and `B` |
| `arms` | Object containing exactly those two labels and their identity records |
| `case_ids` | Nonempty unique selection from the public suite |
| `repetitions` | Integer 1–16, declared before execution |
| `trials` | Exactly one record per selected case × arm × repetition, maximum 512 |

Every arm contains exactly `skill_revision` (full 40- or 64-character lowercase Git
hex ID), `skill_snapshot_sha256` (64-character lowercase SHA-256), `model` (exact
model identity), `runtime` (exact runtime version), `settings` (complete configuration
object), `tools` (list of unique `{ "name": "…", "version": "…" }` identities), and
`tool_policy` (the exact permission configuration). Empty `tools` means no tools
were available. Both arms must use identical model, runtime, settings, ordered tool
inventory and policy. Only skill identity differs. Metadata is asserted by the
recorder; the checker cannot verify whether the runtime actually honored it.

Canonical hashes use UTF-8 of `json.dumps(value, sort_keys=True,
separators=(',', ':'), ensure_ascii=False, allow_nan=False)`. The suite and rubric
schemas are `ci-behavior-cases/1` and `ci-behavior-rubric/1`, each with unique `cases`.
Public records contain exactly `id`, `prompt`, `input`; grading records contain
exactly `id`, `criteria`, `critical_failures`. Their case ID inventories must agree.
Hash a complete public case record for each trial's `input_sha256`.

## Trial records

| Field | Required value |
|---|---|
| `case_id`, `arm`, `repetition` | Exact planned identity; repetitions start at 1 |
| `sequence` | Actual execution position, a unique integer from 1 through trial count |
| `session_id` | Unique fresh-session identity, never reused across trials |
| `input_sha256` | Canonical hash of this case's exact public record |
| `status` | `graded` or `blocked` |
| `grade` | `pass` or `fail` for graded; `null` for blocked |
| `critical_failure` | Boolean for graded; `null` for blocked; true must fail |
| `grader_id` | Independent grader identity for graded, distinct from session ID; optional/null for blocked |
| `reason` | Concrete grading rationale or blocker, nonempty |
| `duration_s` | Finite nonnegative measured duration; unknown/null only for blocked |
| `output`, `tool_calls` | Each a raw-artifact record; required for graded, optional/null for blocked |
| `evidence` | Quoted artifact locations, at least one for graded; empty allowed for blocked |

For each case/repetition, determine which arm ran first from `sequence`. Across the
selected pairs, first-arm counts may differ by at most one. Record order before the
experiment and retain execution provenance separately: a complete permutation does
not prove that it was predeclared or that sessions were isolated.

Raw-artifact records contain exactly `path` and `sha256`. Paths are canonical relative
POSIX paths below `--evidence-root`; absolute paths, parent traversal, escaping
symlinks and reuse of one resolved file are rejected. Hash the original UTF-8 bytes.
Each file is at most 1 MiB; all referenced raw files total at most 32 MiB. Input JSON
files are at most 8 MiB. Keep credentials and private data out of retained artifacts.

Each evidence entry contains exactly `artifact` (`output` or `tool_calls`), `start_line`,
`end_line` (inclusive, one-based), and `quote`. The quote must occur in those lines of
that trial's hashed artifact. This checks traceability, not whether the quoted
material justifies the grade. Strings are nonempty and at most 1,024 characters;
longer raw responses belong in artifacts, not `reason` or `quote`.

## Interpretation

The summary preserves every planned trial in its denominator. A pair with either
arm blocked is inconclusive; pass→fail is a regression, fail→pass an improvement,
and the other completed pairs retain both-pass/both-fail counts. It reports selected
and total suite cases separately. A full ledger with human passes still cannot prove
source authenticity, robust model behavior, CI admission safety or a speedup.
