---
name: validate
description: Run ML feature governance validation, inspect architecture violations, and report precise fixes without changing generated outputs by hand.
---

# Validate Feature Platform

Run:

```bash
python .feature-platform/tools/featurectl.py doctor --project-dir .
python .feature-platform/tools/featurectl.py validate --project-dir .
```

Then inspect relevant repository tests. If the canonical validation passes but engine implementations diverge, compare
normalized outputs using the project's golden-data differential tests.

Classify failures as:

1. contract/schema;
2. DAG/dependency;
3. fitted-state/reproducibility;
4. leakage/time semantics;
5. type/precision/backend capability;
6. generated-output drift;
7. cross-engine semantic divergence.

Fix the canonical source, adapter, or generator responsible for the failure. Never hand-edit `feature-platform/generated/`.
