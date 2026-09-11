# 0.5.0

- Integrate Iceberg docs, table-scoped field identity, physical-schema checks, and independent snapshot pins.
- Preserve canonical required types/nullability and separate fitted normalization nodes per user clarification.
- Add static storage/dataset validation and metadata guidance shared across clients.
- Keep catalog verification and retention explicitly outside static validation.

# Changelog

## 0.4.0 — Cross-client edition

- Added native `.cursor-plugin/plugin.json` and `.codex-plugin/plugin.json` manifests alongside the Claude manifest.
- Added native Cursor rules and lower-camel-case hook events while retaining Claude/Codex hook compatibility.
- Normalized hook payloads across Claude direct writes, Cursor file-edit events, and Codex `apply_patch` commands.
- Converted the three specialist review procedures into portable skills; Claude and Cursor agents remain optional.
- Made workflow skills provider-neutral and standardized commands on a project-local validator.
- Added an idempotent `AGENTS.md` governance block, a managed Cursor project rule, and upgrade-safe managed-file hashes.
- Added cross-client manifests, hook, bootstrap, and fallback tests.

## 0.3.0 — Standalone edition

- Removed every external workflow installer, integration directory, preset, project-memory bridge, provider-settings
  merge, upstream download, managed framework file, and framework health check.
- Replaced the external lifecycle layer with a compact native change workflow built from Claude skills, deterministic
  hooks, versioned change contracts, specialist review agents, and explicit human approval for breaking migrations.
- Replaced the upstream lock with `.feature-platform/governance.lock.json`.
- Added source/entity binding checks, reference-backend type coverage, and change-contract validation.
- Kept the canonical feature DAG, feature sets, experiments, backend matrix, protected generated/state paths, and
  portability/leakage/reproducibility policy intact.
