#!/usr/bin/env python3
import subprocess
import sys
from pathlib import Path

from hook_utils import edited_paths, project_root, read_payload

payload = read_payload()
project = project_root(payload)

canonical_edit = False
for path in edited_paths(payload, project):
    try:
        rel = path.relative_to(project)
    except ValueError:
        continue
    if not rel.parts or rel.parts[0] != "feature-platform":
        continue
    if len(rel.parts) > 1 and rel.parts[1] == "generated":
        continue
    if path.suffix.lower() in {".yaml", ".yml", ".json", ".toml"}:
        canonical_edit = True
        break

if not canonical_edit:
    sys.exit(0)

validator = Path(__file__).with_name("featurectl.py")
proc = subprocess.run(
    [sys.executable, str(validator), "validate", "--project-dir", str(project)],
    text=True,
    capture_output=True,
)
if proc.returncode != 0:
    msg = (proc.stdout + "\n" + proc.stderr).strip()
    print("ML Feature Governance validation failed after the edit:\n" + msg, file=sys.stderr)
    sys.exit(2)

sys.exit(0)
