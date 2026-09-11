#!/usr/bin/env python3
"""Install the standalone feature-governance scaffold into a repository.

The initializer is deliberately local-only: it copies missing canonical templates,
preserves user-owned files, records the installed contract version, and validates
the resulting project. It does not install or invoke an external workflow system.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = PLUGIN_ROOT / "templates" / "project"
PLUGIN_NAME = "ml-feature-governance"
PLUGIN_VERSION = "0.5.0"
CONTRACT_VERSION = "1.0"
AGENTS_START = "<!-- ml-feature-governance:start -->"
AGENTS_END = "<!-- ml-feature-governance:end -->"


class ManagedFileConflict(RuntimeError):
    pass


def template_files(root: Path) -> Iterable[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def copy_missing(src_root: Path, dst_root: Path) -> tuple[list[str], list[str]]:
    """Copy template files that do not exist and preserve every existing file."""
    created: list[str] = []
    preserved: list[str] = []
    for src in template_files(src_root):
        rel = src.relative_to(src_root)
        dst = dst_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            preserved.append(str(rel))
            continue
        shutil.copy2(src, dst)
        created.append(str(rel))
    return created, preserved


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_governance_lock(project: Path) -> dict[str, object]:
    path = project / ".feature-platform" / "governance.lock.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def sync_managed_file(src: Path, dst: Path, previous_hash: str | None) -> str:
    """Install or safely upgrade a plugin-owned project file."""
    new_hash = file_sha256(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        current_hash = file_sha256(dst)
        if current_hash == new_hash:
            return "unchanged"
        if previous_hash is None or current_hash != previous_hash:
            raise ManagedFileConflict(
                f"refusing to overwrite locally modified managed file: {dst}"
            )
    shutil.copy2(src, dst)
    return "updated" if previous_hash else "created"


def upsert_agents_block(path: Path, body: str) -> str:
    """Insert or replace only the marked governance section in AGENTS.md."""
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    has_start = AGENTS_START in existing
    has_end = AGENTS_END in existing
    if has_start != has_end:
        raise ManagedFileConflict(f"unmatched ML governance markers in {path}")

    block = f"{AGENTS_START}\n{body.strip()}\n{AGENTS_END}"
    if has_start:
        before, rest = existing.split(AGENTS_START, 1)
        _, after = rest.split(AGENTS_END, 1)
        updated = before.rstrip() + "\n\n" + block + after
    else:
        updated = existing.rstrip() + ("\n\n" if existing.strip() else "") + block + "\n"
    if updated == existing:
        return "unchanged"
    path.write_text(updated, encoding="utf-8")
    return "updated" if existing else "created"


def governance_lock(managed_files: dict[str, str] | None = None) -> dict[str, object]:
    return {
        "plugin": {"name": PLUGIN_NAME, "version": PLUGIN_VERSION},
        "edition": "standalone",
        "clients": ["claude-code", "cursor", "openai-codex"],
        "contract_version": CONTRACT_VERSION,
        "canonical_root": "feature-platform",
        "generated_root": "feature-platform/generated",
        "managed_files": managed_files or {},
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def write_governance_lock(project: Path, managed_files: dict[str, str] | None = None) -> Path:
    path = project / ".feature-platform" / "governance.lock.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(governance_lock(managed_files), indent=2) + "\n", encoding="utf-8")
    return path


def validate(project: Path) -> int:
    validator = project / ".feature-platform" / "tools" / "featurectl.py"
    proc = subprocess.run(
        [sys.executable, str(validator), "validate", "--project-dir", str(project)],
        text=True,
    )
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize standalone ML Feature Governance")
    parser.add_argument("--project-dir", default=".")
    args = parser.parse_args()

    project = Path(args.project_dir).resolve()
    project.mkdir(parents=True, exist_ok=True)

    feature_created, feature_preserved = copy_missing(
        TEMPLATE_ROOT / "feature-platform", project / "feature-platform"
    )
    metadata_created, metadata_preserved = copy_missing(
        TEMPLATE_ROOT / ".feature-platform", project / ".feature-platform"
    )

    previous = read_governance_lock(project)
    previous_managed = previous.get("managed_files")
    if not isinstance(previous_managed, dict):
        previous_managed = {}

    managed_sources = {
        ".feature-platform/tools/featurectl.py": PLUGIN_ROOT / "scripts" / "featurectl.py",
        ".cursor/rules/ml-feature-governance.mdc": (
            PLUGIN_ROOT / "templates" / "client" / "cursor" / "feature-governance.mdc"
        ),
    }
    managed_states: list[str] = []
    managed_hashes: dict[str, str] = {}
    try:
        for rel, src in managed_sources.items():
            old_hash = previous_managed.get(rel)
            if not isinstance(old_hash, str):
                old_hash = None
            state = sync_managed_file(src, project / rel, old_hash)
            managed_states.append(f"{rel}: {state}")
            managed_hashes[rel] = file_sha256(src)

        agents_body = (PLUGIN_ROOT / "policies" / "project-agents-block.md").read_text(encoding="utf-8")
        agents_state = upsert_agents_block(project / "AGENTS.md", agents_body)
    except ManagedFileConflict as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if validate(project) != 0:
        print(
            "Initialization copied only missing files, but the resulting canonical project is invalid. "
            "Resolve the reported conflicts or contract violations, then rerun initialization.",
            file=sys.stderr,
        )
        return 2

    lock = write_governance_lock(project, managed_hashes)
    print(
        "ML Feature Governance initialization complete "
        f"({len(feature_created) + len(metadata_created)} created, "
        f"{len(feature_preserved) + len(metadata_preserved)} preserved)"
    )
    print(f"AGENTS.md governance block: {agents_state}")
    for state in managed_states:
        print(f"managed file: {state}")
    print(f"governance lock: {lock.relative_to(project)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
