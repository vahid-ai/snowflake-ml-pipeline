from __future__ import annotations

import json
import py_compile
import unittest
from pathlib import Path

import yaml

PLUGIN = Path(__file__).resolve().parents[1]


def frontmatter(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise AssertionError(f"{path} has no YAML frontmatter")
    raw = text.split("---\n", 2)[1]
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise AssertionError(f"{path} frontmatter is not a mapping")
    return data


class PluginStructureTests(unittest.TestCase):
    def test_all_client_manifests_are_valid_json(self) -> None:
        manifests = {
            "claude": PLUGIN / ".claude-plugin" / "plugin.json",
            "cursor": PLUGIN / ".cursor-plugin" / "plugin.json",
            "codex": PLUGIN / ".codex-plugin" / "plugin.json",
        }
        for path in manifests.values():
            manifest = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["name"], "ml-feature-governance")
            self.assertEqual(manifest["version"], "0.5.0")

        cursor = json.loads(manifests["cursor"].read_text(encoding="utf-8"))
        self.assertEqual(cursor["hooks"], "./cursor/hooks.json")
        self.assertEqual(cursor["rules"], "./rules/")

        codex = json.loads(manifests["codex"].read_text(encoding="utf-8"))
        self.assertEqual(codex["skills"], "./skills/")
        self.assertEqual(codex["interface"]["category"], "Productivity")

    def test_claude_codex_hooks_are_valid_json(self) -> None:
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(set(hooks["hooks"]), {"SessionStart", "PreToolUse", "PostToolUse"})
        for groups in hooks["hooks"].values():
            for group in groups:
                for hook in group["hooks"]:
                    self.assertEqual(hook["type"], "command")
                    self.assertIn("${CLAUDE_PLUGIN_ROOT}", hook["command"])

    def test_cursor_hooks_and_rule_are_valid(self) -> None:
        hooks = json.loads((PLUGIN / "cursor" / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(hooks["version"], 1)
        self.assertEqual(
            set(hooks["hooks"]),
            {"sessionStart", "preToolUse", "postToolUse", "afterFileEdit", "afterTabFileEdit"},
        )
        for entries in hooks["hooks"].values():
            for entry in entries:
                self.assertTrue(entry["command"].startswith("python ./scripts/"))

        rule = frontmatter(PLUGIN / "rules" / "ml-feature-governance.mdc")
        self.assertTrue(rule["alwaysApply"])

    def test_skill_frontmatter_names_match_directories(self) -> None:
        for skill_file in sorted((PLUGIN / "skills").glob("*/SKILL.md")):
            data = frontmatter(skill_file)
            self.assertEqual(data.get("name"), skill_file.parent.name)
            self.assertIsInstance(data.get("description"), str)
            self.assertTrue(str(data["description"]).strip())

    def test_agent_frontmatter_is_parseable(self) -> None:
        for agent_file in sorted((PLUGIN / "agents").glob("*.md")):
            data = frontmatter(agent_file)
            self.assertEqual(data.get("name"), agent_file.stem)
            self.assertIsInstance(data.get("description"), str)
            self.assertIn("Read", data.get("tools", ""))

    def test_python_scripts_compile(self) -> None:
        for script in sorted((PLUGIN / "scripts").glob("*.py")):
            py_compile.compile(str(script), doraise=True)

    def test_shared_skills_do_not_require_vendor_environment_variables(self) -> None:
        forbidden = ("CLAUDE_PLUGIN_ROOT", "CLAUDE_PROJECT_DIR", "CURSOR_PLUGIN_ROOT", "CODEX_HOME")
        for skill_file in sorted((PLUGIN / "skills").glob("*/SKILL.md")):
            text = skill_file.read_text(encoding="utf-8")
            self.assertFalse(any(term in text for term in forbidden), skill_file)


if __name__ == "__main__":
    unittest.main()
