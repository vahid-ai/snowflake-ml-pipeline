# Repository plugins

Both plugins apply to work throughout `snowflake-ml-pipeline`. Their source packages live in
`plugins/ml-feature-governance/` and `plugins/agent-code-intelligence/`; there is no repository `ml/` scope.

## Shared repository behavior

- Root `AGENTS.md` directs Codex and other compatible agents to both plugins' skills and policies.
- Root `CLAUDE.md` imports those instructions. `.claude/settings.json` registers this checkout's marketplace
  and enables both plugins for this project, subject to the client's normal trust/install prompts.
- Cursor's always-on project rules point to the same guidance.
- ML governance is initialized at the root: `feature-platform/` holds canonical contracts and
  `.feature-platform/tools/featurectl.py` validates them. The initial source and feature definitions are starter
  examples supplied by the plugin. They do not assert that the existing LAMDA tables have those schemas.
- The GitHub plugin-check workflow runs both plugin suites, repository wiring checks, and root contract validation.

This setup does not start optional MCP servers or enable failure-data capture. Native plugin installation and hooks
remain subject to each client's trust settings; repository instructions and CI validation also work without them.

## Native Codex plugin surfaces

The repository catalog is `.agents/plugins/marketplace.json`. To install its native plugin surfaces in a Codex
environment, add this repository as a marketplace and select both plugins in that environment's plugin manager.
The catalog alone does not install plugins into every contributor's account. Root `AGENTS.md` already provides
the repository-wide workflow for agents that read repository instructions.

## Validation

From the repository root, using Python 3.11 or newer:

```bash
python -m pip install -r plugins/ml-feature-governance/requirements.txt -r plugins/agent-code-intelligence/requirements-dev.txt
python -m unittest discover -s plugins/ml-feature-governance/tests -v
python -m unittest discover -s plugins/agent-code-intelligence/tests -v
python -m unittest discover -s tests -p test_repository_plugins.py -v
python .feature-platform/tools/featurectl.py doctor --project-dir .
python .feature-platform/tools/featurectl.py validate --project-dir .
```

When updating the governance plugin, refresh managed root files with
`python plugins/ml-feature-governance/scripts/bootstrap_project.py --project-dir .`. The initializer preserves
existing canonical definitions and instructions outside its marked governance block, and detects locally edited
managed files. Re-run validation after updates.
