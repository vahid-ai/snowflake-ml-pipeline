from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
GUARD = PLUGIN / "scripts" / "guard_write.py"
POST_WRITE = PLUGIN / "scripts" / "validate_after_write.py"
SESSION = PLUGIN / "scripts" / "session_context.py"


class HookTests(unittest.TestCase):
    def run_guard(
        self, project: Path, file_path: Path | None = None, payload: dict[str, object] | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["CLAUDE_PROJECT_DIR"] = str(project)
        if payload is None:
            payload = {"tool_input": {"file_path": str(file_path)}}
        return subprocess.run(
            [sys.executable, str(GUARD)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
        )

    def test_generated_output_write_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            proc = self.run_guard(project, project / "feature-platform" / "generated" / "dbt" / "model.sql")
            self.assertEqual(proc.returncode, 2)
            self.assertIn("generated/managed artifact", proc.stderr)

    def test_runtime_state_and_governance_lock_writes_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            state = self.run_guard(project, project / ".feature-platform" / "state" / "fit.json")
            lock = self.run_guard(project, project / ".feature-platform" / "governance.lock.json")
            self.assertEqual(state.returncode, 2)
            self.assertEqual(lock.returncode, 2)

    def test_managed_project_tools_and_cursor_rule_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            tool = self.run_guard(project, project / ".feature-platform" / "tools" / "featurectl.py")
            rule = self.run_guard(project, project / ".cursor" / "rules" / "ml-feature-governance.mdc")
            self.assertEqual(tool.returncode, 2)
            self.assertEqual(rule.returncode, 2)

    def test_codex_apply_patch_targets_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            payload = {
                "cwd": str(project),
                "tool_name": "apply_patch",
                "tool_input": {
                    "command": "*** Begin Patch\n*** Update File: feature-platform/generated/model.sql\n@@\n-old\n+new\n*** End Patch"
                },
            }
            proc = self.run_guard(project, payload=payload)
            self.assertEqual(proc.returncode, 2)

    def test_canonical_definition_write_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            proc = self.run_guard(project, project / "feature-platform" / "features" / "network.yaml")
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_post_write_rejects_invalid_canonical_edit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            shutil.copytree(PLUGIN / "templates" / "project" / "feature-platform", project / "feature-platform")
            path = project / "feature-platform" / "features" / "network.yaml"
            path.write_text(path.read_text().replace("type: uint16", "type: int", 1))
            env = dict(os.environ)
            env["CLAUDE_PROJECT_DIR"] = str(project)
            proc = subprocess.run(
                [sys.executable, str(POST_WRITE)],
                input=json.dumps({"tool_input": {"file_path": str(path)}}),
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertIn("validation failed after the edit", proc.stderr)
            self.assertIn("invalid/ambiguous output type", proc.stderr)

    def test_post_write_accepts_valid_canonical_edit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            shutil.copytree(PLUGIN / "templates" / "project" / "feature-platform", project / "feature-platform")
            path = project / "feature-platform" / "features" / "network.yaml"
            env = dict(os.environ)
            env["CLAUDE_PROJECT_DIR"] = str(project)
            proc = subprocess.run(
                [sys.executable, str(POST_WRITE)],
                input=json.dumps({"tool_input": {"file_path": str(path)}}),
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_cursor_after_file_edit_rejects_invalid_canonical_edit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            shutil.copytree(PLUGIN / "templates" / "project" / "feature-platform", project / "feature-platform")
            path = project / "feature-platform" / "features" / "network.yaml"
            path.write_text(path.read_text().replace("type: uint16", "type: int", 1))
            proc = subprocess.run(
                [sys.executable, str(POST_WRITE)],
                input=json.dumps(
                    {
                        "cursor_version": "1.7.2",
                        "workspace_roots": [str(project)],
                        "hook_event_name": "afterFileEdit",
                        "file_path": str(path),
                    }
                ),
                text=True,
                capture_output=True,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertIn("invalid/ambiguous output type", proc.stderr)

    def test_cursor_session_context_uses_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            (project / "feature-platform").mkdir()
            proc = subprocess.run(
                [sys.executable, str(SESSION)],
                input=json.dumps(
                    {
                        "cursor_version": "1.7.2",
                        "workspace_roots": [str(project)],
                        "hook_event_name": "sessionStart",
                    }
                ),
                text=True,
                capture_output=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            output = json.loads(proc.stdout)
            self.assertIn("ML FEATURE GOVERNANCE IS ACTIVE", output["additional_context"])


if __name__ == "__main__":
    unittest.main()
