"""Integration checks for the repository's plugin catalogs and managed files."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
NAMES = {"ml-feature-governance", "agent-code-intelligence"}


class RepositoryPluginTests(unittest.TestCase):
    def test_marketplaces_resolve_both_plugins_from_repository_root(self):
        for catalog_path, manifest_dir in (
            (".claude-plugin/marketplace.json", ".claude-plugin"),
            (".agents/plugins/marketplace.json", ".codex-plugin"),
        ):
            catalog = json.loads((ROOT / catalog_path).read_text())
            self.assertEqual(catalog["name"], "snowflake-ml-pipeline")
            self.assertEqual({p["name"] for p in catalog["plugins"]}, NAMES)
            for plugin in catalog["plugins"]:
                source = plugin["source"]
                path = source["path"] if isinstance(source, dict) else source
                manifest = json.loads((ROOT / path / manifest_dir / "plugin.json").read_text())
                self.assertEqual(manifest["name"], plugin["name"])

    def test_claude_project_settings_enable_repository_plugins(self):
        settings = json.loads((ROOT / ".claude/settings.json").read_text())
        self.assertEqual(settings["extraKnownMarketplaces"]["snowflake-ml-pipeline"]["source"],
                         {"source": "directory", "path": "."})
        self.assertEqual(settings["enabledPlugins"],
                         {f"{name}@snowflake-ml-pipeline": True for name in NAMES})
        self.assertIn("@AGENTS.md", (ROOT / "CLAUDE.md").read_text())

    def test_managed_governance_files_match_plugin_and_lock(self):
        lock = json.loads((ROOT / ".feature-platform/governance.lock.json").read_text())
        sources = {
            ".feature-platform/tools/featurectl.py": "scripts/featurectl.py",
            ".cursor/rules/ml-feature-governance.mdc": "templates/client/cursor/feature-governance.mdc",
        }
        for target, source in sources.items():
            data = (ROOT / target).read_bytes()
            self.assertEqual(data, (ROOT / "plugins/ml-feature-governance" / source).read_bytes())
            self.assertEqual(hashlib.sha256(data).hexdigest(), lock["managed_files"][target])

    def test_governance_targets_the_repository_root(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / ".feature-platform/tools/featurectl.py"),
             "validate", "--project-dir", str(ROOT)],
            text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
