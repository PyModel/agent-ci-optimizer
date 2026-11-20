# Behavior evaluation

Before replacing the core skill or changing a gate recommendation, snapshot
the exact skill tree and compare with a fixed baseline. Use identical task and
repository fixtures, model/runtime settings, and tool permissions. Use fresh
isolated sessions and a balanced execution order. Give the executing agent only
[case prompts](prompts.md) and their public inputs, never the [grading rubric](rubric.md).
Capture raw outputs, exact revisions, duration, tool calls and limitations before
an independent grader sees blinded arm labels. Record blocked cases separately;
do not reclassify them as passed or quietly drop them from the denominator.

Grade against task behavior and evidence, not keywords. Report paired passes,
regressions, unsafe or false greens, and uncertainty; do not claim efficiency
gains across hosts from one experiment. Start with synthetic/offline inputs.
No model comparison is represented by adding this protocol and its cases.
