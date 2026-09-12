---
name: experiment
description: Define or review ML feature ablations, feature combinations, alternate transforms, feature counts, or backend comparisons as reproducible versioned experiments.
---

# Feature Experiment Governance

Use the user's supplied goal as the experiment goal. If the client exposes arguments as `$ARGUMENTS`, consume them.

Create or update an experiment definition under `feature-platform/experiments/`. Experiments select existing immutable
feature versions or propose new feature versions; they do not mutate a canonical feature definition in place.

Every experiment MUST resolve to a manifest that pins:

- base feature-set ID/version;
- exact included/excluded feature versions;
- transform variant/version where changed;
- source dataset snapshot/version;
- train/validation/test split definition/version;
- fitted transform artifact versions after fitting;
- backend and backend capability profile;
- code commit/environment lock identifier when executing;
- deterministic seed(s);
- expected model input ordering, dtypes, and shapes.

For ablations, prefer declarative variants such as `include`, `exclude`, `replace`, and parameter grids over conditional
code paths spread across notebooks.

If a parameter changes feature semantics (for example hash bucket count, clipping threshold, window duration, tokenizer,
embedding model, normalization method), create a new feature version or explicit transform variant. Do not silently
reuse the old feature identity.

Run `python .feature-platform/tools/featurectl.py validate --project-dir .` after editing the experiment.

If an experiment introduces new feature semantics rather than selecting existing versions, also create a change contract
under `feature-platform/changes/` and follow the canonical-first change workflow.

## Iceberg metadata

For Iceberg work, read `feature-platform/docs/iceberg-metadata.md` in the project (or
`templates/project/feature-platform/docs/iceberg-metadata.md` from the plugin root before initialization).
Apply the physical-schema, field-identity, documentation, and snapshot contract.
