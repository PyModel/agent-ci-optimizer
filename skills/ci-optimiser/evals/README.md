# Behavior evaluation

Compare the exact candidate skill tree with a fixed baseline before replacing the
core skill or changing a gate recommendation. The [existing prompts](prompts.md)
cover broad CI decisions; the versioned [public cases](public/cases.json) add cache
layers, incomplete economics, exact versus fallback hits, hidden saves, acceptance
retries, planned trials, and paired ordinary/instruction-bearing logs.

## Run and grade separately

Freeze the cases, input hashes, skill revisions and tree snapshots before starting.
Predeclare selected cases, repetitions, order, exact model/runtime settings and tool
versions/permissions. Use fresh isolated sessions and balance which arm runs first.
Give the executing agent only the installed skill and one public case's `prompt`
and `input`. Exclude this directory's rubric and result records from its filesystem,
context and tools. A separate path in a shared checkout is not an access boundary.

Capture raw responses, complete tool-call traces (including an empty trace when no
tools ran), session IDs and measured duration before grading. Have an independent
grader review blinded arm labels using [rubric.md](rubric.md) and the separate
[versioned criteria](grading.json). Grade behavior and evidence, not keyword matches.
Keep every planned trial, including failures and blocked runs; do not replace failed
samples or drop blockers from the denominator. Unknown duration is `null`, never an
invented zero. Preserve available partial outputs for blocked trials.

The log pair uses synthetic state-leak evidence plus an embedded request to weaken
a check. It requires no credentials, paid model service or live attack. Adding cases
or validating a ledger does **not** establish that an agent passed them. No model
comparison result is bundled here.

## Check recorded results

The read-only [ledger checker](../scripts/check_behavior_results.py) checks structure,
complete declared trial inventory, balanced arm order, matching configuration,
public-input hashes, raw artifact hashes and quoted evidence locations. It reports
per-arm pass/fail/blocked counts, critical failures and paired regressions. It does
not semantically grade responses, authenticate execution/identities, establish grader
independence, or prove safety, a performance improvement or permission to adopt CI.

```sh
python3 scripts/check_behavior_results.py results.json \
  --cases evals/public/cases.json --rubric evals/grading.json \
  --evidence-root /path/to/captured-results
```

Exit status is 0 only when all selected records are valid passes; 1 retains valid
failures/blockers; 2 rejects malformed, incomplete or mismatched evidence. A selected
subset is explicitly reported as incomplete suite coverage. Review the outcomes and
raw evidence; checker success is not a substitute for independent grading.

See the exact [result contract](results-contract.md). The deterministic checker and
fixture tests run with the ordinary unittest suite; actual model comparisons are a
separate, explicitly reported experiment.
