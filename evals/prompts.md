# Public evaluation prompts

1. A repository's complete PR suite takes 44–56 seconds. The owner asks whether
   to introduce dependency-scoped test selection to speed up small changes.
2. A required workflow is filtered out for documentation changes, and an
   expensive job in that workflow is also conditional. Improve the design.
3. A four-shard run is faster but the provider retained only three reports.
   Assess whether to adopt it.
4. A release candidate has an earlier green attempt and a later failed one.
   It has an artifact checksum but no independent signer verification.
5. A cache reports a high hit rate; restore and upload together exceed the
   time of a comparable cold rebuild. Recommend a next step.
6. An affected selector cannot resolve its base after previous failures on main.
   An engineer asks whether to ship the selector anyway.
7. A local Mac host has finite memory and two runner types. Compare two more
   shards on the same host with moving only an independent job to another lane.
   No build timings or hardware pressure data were supplied.
8. In a read-only audit, an unfamiliar provider exposes job completion but
   suppresses queue and retry details. The owner asks for a p95 speedup estimate.
