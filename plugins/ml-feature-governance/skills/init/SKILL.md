---
name: init
description: Initialize a repository for Claude Code, Cursor, or OpenAI Codex with the standalone ML feature-platform scaffold, repository instructions, local governance metadata, and deterministic validation.
---

# Initialize Cross-Client ML Feature Governance

Perform initialization in the current repository. Preserve existing user-owned files and report validation conflicts
instead of silently overwriting them.

1. Inspect whether `feature-platform/` and `.feature-platform/` already exist.
2. Confirm Python 3 and PyYAML 6 are available.
3. Resolve `../../scripts/bootstrap_project.py` relative to this `SKILL.md`, then run the bundled script against the
   current repository. Do not assume a vendor-specific plugin environment variable. Equivalent command:

```bash
python "<plugin-root>/scripts/bootstrap_project.py" --project-dir .
```

4. The initializer copies only missing canonical scaffold files, installs an upgrade-safe project validator, adds a
   marked governance block to `AGENTS.md`, installs a Cursor project rule, validates the combined project, and writes
   `.feature-platform/governance.lock.json`.
5. Run `featurectl doctor` and `featurectl validate` once more when initialization is being performed inside a larger
   repository automation or CI setup.
6. Do not add unrelated project tooling, modify model/provider settings, or install an external workflow system.
