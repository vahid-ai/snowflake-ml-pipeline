"""Integration checks for the repository's plugin catalogs and managed files."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
NAMES = {"ml-feature-governance", "agent-code-intelligence"}
PONYTAIL_REPO = "DietrichGebert/ponytail"
PONYTAIL_GIT = "https://github.com/DietrichGebert/ponytail.git"


def _source_kind(source: object) -> str:
    if isinstance(source, dict):
        return str(source.get("source", "local"))
    return "local"


# Verify plugin discovery, client activation, managed-file identity, and validation from the
# repository root.
class RepositoryPluginTests(unittest.TestCase):
    def test_marketplaces_resolve_both_plugins_from_repository_root(self):
        for catalog_path, manifest_dir in (
            (".claude-plugin/marketplace.json", ".claude-plugin"),
            (".agents/plugins/marketplace.json", ".codex-plugin"),
        ):
            catalog = json.loads((ROOT / catalog_path).read_text())
            self.assertEqual(catalog["name"], "snowflake-ml-pipeline")
            local_plugins = [p for p in catalog["plugins"] if _source_kind(p["source"]) == "local"]
            self.assertEqual({p["name"] for p in local_plugins}, NAMES)
            for plugin in local_plugins:
                source = plugin["source"]
                path = source["path"] if isinstance(source, dict) else source
                manifest = json.loads((ROOT / path / manifest_dir / "plugin.json").read_text())
                self.assertEqual(manifest["name"], plugin["name"])

    def test_claude_project_settings_enable_repository_plugins(self):
        settings = json.loads((ROOT / ".claude/settings.json").read_text())
        self.assertEqual(settings["extraKnownMarketplaces"], {
            "snowflake-ml-pipeline": {"source": {"source": "directory", "path": "."}},
        })
        enabled = {f"{name}@snowflake-ml-pipeline": True for name in NAMES}
        enabled["ponytail@snowflake-ml-pipeline"] = True
        self.assertEqual(settings["enabledPlugins"], enabled)
        self.assertIn("@AGENTS.md", (ROOT / "CLAUDE.md").read_text())

        claude_catalog = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
        remote = [p for p in claude_catalog["plugins"] if p["name"] == "ponytail"]
        self.assertEqual(len(remote), 1)
        self.assertEqual(remote[0]["source"], {"source": "github", "repo": PONYTAIL_REPO})

    def test_ponytail_is_enabled_for_codex_and_cursor(self):
        catalog = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text())
        remote = [p for p in catalog["plugins"] if p["name"] == "ponytail"]
        self.assertEqual(len(remote), 1)
        self.assertEqual(remote[0]["source"], {
            "source": "url",
            "url": PONYTAIL_GIT,
            "ref": "main",
        })
        self.assertEqual(remote[0]["policy"]["installation"], "INSTALLED_BY_DEFAULT")

        config = (ROOT / ".codex/config.toml").read_text(encoding="utf-8")
        self.assertIn('[plugins."ponytail@snowflake-ml-pipeline"]', config)
        self.assertIn("enabled = true", config)
        self.assertNotIn("[marketplaces.ponytail]", config)
        self.assertNotIn('[plugins."ponytail@ponytail"]', config)

        rule = (ROOT / ".cursor/rules/ponytail.mdc").read_text(encoding="utf-8")
        self.assertIn("alwaysApply: true", rule)
        self.assertIn("YAGNI", rule)
        self.assertIn("## Ponytail", (ROOT / "AGENTS.md").read_text(encoding="utf-8"))

    def test_managed_governance_files_match_plugin_and_lock(self):
        lock = json.loads((ROOT / ".feature-platform/governance.lock.json").read_text())
        sources = {
            ".feature-platform/tools/featurectl.py": "scripts/featurectl.py",
            ".cursor/rules/ml-feature-governance.mdc": "templates/client/cursor/feature-governance.mdc",
        }
        for target, source in sources.items():
            # Compare exact bytes because bootstrap uses these hashes to distinguish managed
            # files from local modifications.
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
