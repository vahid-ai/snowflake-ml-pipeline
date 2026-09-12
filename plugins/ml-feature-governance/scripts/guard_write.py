#!/usr/bin/env python3
import sys

from hook_utils import edited_paths, inside, project_root, read_payload

payload = read_payload()
project = project_root(payload)
paths = edited_paths(payload, project)
if not paths:
    sys.exit(0)

protected = [
    (project / "feature-platform" / "generated").resolve(),
    (project / ".feature-platform" / "state").resolve(),
    (project / ".feature-platform" / "tools").resolve(),
]
protected_files = {
    (project / ".feature-platform" / "governance.lock.json").resolve(),
    (project / ".cursor" / "rules" / "ml-feature-governance.mdc").resolve(),
}

if any(path in protected_files or any(inside(path, root) for root in protected) for path in paths):
    print(
        "Blocked by ML Feature Governance: this is a generated/managed artifact. "
        "Modify the canonical feature definition, generator, adapter, or bootstrap source instead.",
        file=sys.stderr,
    )
    sys.exit(2)

sys.exit(0)
