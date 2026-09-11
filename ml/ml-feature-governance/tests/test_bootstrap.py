from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_PATH = ROOT / "scripts" / "bootstrap_project.py"
SPEC = importlib.util.spec_from_file_location("bootstrap_project", BOOTSTRAP_PATH)
assert SPEC and SPEC.loader
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    def test_copy_missing_preserves_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            temp = Path(td)
            src = temp / "src"
            dst = temp / "dst"
            (src / "nested").mkdir(parents=True)
            (dst / "nested").mkdir(parents=True)
            (src / "nested" / "policy.yaml").write_text("template\n", encoding="utf-8")
            (src / "new.yaml").write_text("new\n", encoding="utf-8")
            (dst / "nested" / "policy.yaml").write_text("user-owned\n", encoding="utf-8")

            created, preserved = bootstrap.copy_missing(src, dst)

            self.assertEqual(created, ["new.yaml"])
            self.assertEqual(preserved, ["nested/policy.yaml"])
            self.assertEqual((dst / "nested" / "policy.yaml").read_text(), "user-owned\n")

    def test_governance_lock_is_local_and_versioned(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            path = bootstrap.write_governance_lock(project)
            lock = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(lock["plugin"]["name"], "ml-feature-governance")
            self.assertEqual(lock["plugin"]["version"], "0.5.0")
            self.assertEqual(lock["edition"], "standalone")
            self.assertEqual(lock["contract_version"], "1.0")
            self.assertEqual(lock["clients"], ["claude-code", "cursor", "openai-codex"])
            self.assertNotIn("upstreams", lock)

    def test_full_initialization_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            command = [sys.executable, str(BOOTSTRAP_PATH), "--project-dir", str(project)]
            first = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            user_file = project / "feature-platform" / "docs" / "architecture-principles.md"
            user_file.write_text("user-owned policy\n", encoding="utf-8")

            second = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertEqual(user_file.read_text(encoding="utf-8"), "user-owned policy\n")
            self.assertTrue((project / ".feature-platform" / "governance.lock.json").exists())
            self.assertTrue((project / "feature-platform" / "changes" / "README.md").exists())
            self.assertTrue((project / ".feature-platform" / "tools" / "featurectl.py").exists())
            self.assertTrue((project / ".cursor" / "rules" / "ml-feature-governance.mdc").exists())
            agents = (project / "AGENTS.md").read_text(encoding="utf-8")
            self.assertEqual(agents.count(bootstrap.AGENTS_START), 1)
            self.assertEqual(agents.count(bootstrap.AGENTS_END), 1)

    def test_agents_block_preserves_existing_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            agents = project / "AGENTS.md"
            agents.write_text("# Existing instructions\n\nKeep this text.\n", encoding="utf-8")
            command = [sys.executable, str(BOOTSTRAP_PATH), "--project-dir", str(project)]
            proc = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            text = agents.read_text(encoding="utf-8")
            self.assertIn("Keep this text.", text)
            self.assertEqual(text.count(bootstrap.AGENTS_START), 1)

    def test_locally_modified_managed_file_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            command = [sys.executable, str(BOOTSTRAP_PATH), "--project-dir", str(project)]
            first = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            tool = project / ".feature-platform" / "tools" / "featurectl.py"
            tool.write_text("# locally modified\n", encoding="utf-8")

            second = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(second.returncode, 2)
            self.assertIn("refusing to overwrite locally modified managed file", second.stderr)
            self.assertEqual(tool.read_text(encoding="utf-8"), "# locally modified\n")

    def test_package_has_no_external_workflow_references(self) -> None:
        forbidden = (
            "ai" + "dlc",
            "ai" + "-dlc",
            "spec" + "kit",
            "spec" + " kit",
        )
        offenders: list[str] = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix.lower() in {".pyc", ".zip"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
            if any(term in text for term in forbidden):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
