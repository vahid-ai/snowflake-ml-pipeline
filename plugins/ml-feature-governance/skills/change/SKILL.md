---
name: change
description: Start or continue a governed ML feature, transformation, feature-set, backend, training-data, or pipeline change using the cross-client standalone workflow.
---

# Governed ML Feature Change

Use the user's supplied intent as the requested change. If the client exposes arguments as `$ARGUMENTS`, consume them.
This is a native plugin workflow; do not require or invoke an external
planning or delivery framework.

## 1. Establish the baseline

- Load the `architecture` skill.
- If `feature-platform/` is missing, initialize the repository first.
- Run `featurectl doctor` and `featurectl validate` before editing. Keep pre-existing failures distinct from the change.
- Inspect the canonical definitions and the consumers affected by the requested intent.

## 2. Classify and contract the change

Classify it as one or more of: implementation-only, additive semantic, breaking semantic, backend support, migration,
or experiment-only.

For any semantic, backend, or migration change, create or update a versioned YAML contract under
`feature-platform/changes/`. Record:

- intent and change classification;
- affected and proposed `id@version` feature or feature-set references;
- source, entity, event-time, and availability-time impact;
- semantic, logical/storage, and model-representation impact;
- fitted-state and leakage implications;
- backend capability and physical-type implications;
- compatibility classification and consumer inventory;
- migration/backfill/dual-read strategy when compatibility is affected;
- required validation, golden differential tests, and review passes.

If the change is breaking, stop after the contract and impact analysis until the user explicitly approves the migration
strategy. Do not treat a request to inspect, plan, diagnose, or review as implementation approval.

## 3. Implement canonical-first

1. Add a new feature version when semantics change; do not mutate an existing identity in place.
2. Update the canonical typed DAG, feature set, experiment, source, and capability declarations.
3. Update compiler, adapter, or plugin implementations.
4. Regenerate derived outputs last.

Never patch generated dbt, SQL, Spark, Snowflake, Feast, MLflow, or schema output to make a test pass without changing
its canonical source or generator.

## 4. Verify

Run:

```bash
python .feature-platform/tools/featurectl.py validate --project-dir .
```

Then run relevant project tests. Semantic changes require golden-data differential tests across every affected supported
backend after normalization to the canonical Arrow-like schema. Apply exact or declared tolerance comparison policies.

Use the `review-architecture`, `review-leakage`, and `review-portability` skills for independent passes when their
domains are affected. Clients that support bundled specialist agents may use them as an execution convenience, but the
review outcome must not depend on agent support. Resolve blocking findings or present them clearly before completion.

## 5. Completion record

Update the change contract with the resolved feature versions, verification evidence, migration decision, and status.
Report canonical files changed, derived outputs generated, tests run, unresolved risks, and any rollout action that still
requires human authorization.
