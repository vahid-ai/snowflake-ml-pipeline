---
name: review
description: Review an ML feature-platform design or code change for portability, leakage, reproducibility, data typing, transformation-state, and cross-backend correctness.
---

# Feature Platform Architecture Review

Perform three independent passes using these bundled skills:

- `review-architecture`
- `review-portability`
- `review-leakage`

Clients that support bundled specialist agents may use the matching agents, but agent support is optional and must not
be required for a complete review.

Prioritize blocking issues over style. Review canonical feature definitions before generated code.

Required review questions:

- Is there exactly one semantic source of truth?
- Does each feature have immutable identity/version and explicit types?
- Are raw/transformed nodes distinct?
- Is transformation state separated from definition?
- Can every dependency be traced through an acyclic DAG?
- Are time/availability semantics sufficient to prove no training-serving leakage?
- Are model input ordering, dtype, shape, and cardinality deterministic?
- Is backend-specific behavior isolated behind adapters/plugins?
- Are unsupported backend mappings rejected rather than silently coerced?
- Are deterministic hashing and string encoding specified?
- Can the run be reproduced from dataset snapshot + split + transform artifacts + code/environment versions?
- Do differential tests demonstrate semantic equivalence across supported engines?
- Are generated artifacts derived and protected from direct edits?

Run `python .feature-platform/tools/featurectl.py validate --project-dir .` and include its findings in the review.

## Iceberg metadata

For Iceberg work, read `feature-platform/docs/iceberg-metadata.md` in the project (or
`templates/project/feature-platform/docs/iceberg-metadata.md` from the plugin root before initialization).
Apply the physical-schema, field-identity, documentation, and snapshot contract.
