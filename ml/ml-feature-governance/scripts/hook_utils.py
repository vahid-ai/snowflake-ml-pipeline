#!/usr/bin/env python3
"""Shared hook payload helpers for Claude Code, Cursor, and Codex."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

PATCH_PATH = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)


def read_payload() -> dict[str, Any]:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def project_root(payload: dict[str, Any]) -> Path:
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return Path(cwd).resolve()

    roots = payload.get("workspace_roots")
    if isinstance(roots, list) and roots and isinstance(roots[0], str):
        return Path(roots[0]).resolve()

    for name in ("CLAUDE_PROJECT_DIR", "CURSOR_PROJECT_DIR", "CODEX_PROJECT_DIR"):
        value = os.environ.get(name)
        if value:
            return Path(value).resolve()
    return Path.cwd().resolve()


def edited_paths(payload: dict[str, Any], project: Path) -> list[Path]:
    """Extract file targets from direct writes and Codex apply_patch payloads."""
    raw: list[str] = []

    top_level = payload.get("file_path")
    if isinstance(top_level, str) and top_level:
        raw.append(top_level)

    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        for key in ("file_path", "path", "filePath"):
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                raw.append(value)
        command = tool_input.get("command")
        if isinstance(command, str):
            raw.extend(PATCH_PATH.findall(command))

    paths: list[Path] = []
    seen: set[Path] = set()
    for value in raw:
        candidate = Path(value.strip())
        if not candidate.is_absolute():
            candidate = project / candidate
        candidate = candidate.resolve()
        if candidate not in seen:
            seen.add(candidate)
            paths.append(candidate)
    return paths


def inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False
