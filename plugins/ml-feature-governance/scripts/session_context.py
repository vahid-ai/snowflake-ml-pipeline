#!/usr/bin/env python3
import json

from hook_utils import project_root, read_payload

payload = read_payload()
project = project_root(payload)
if (project / "feature-platform").exists():
    context = """ML FEATURE GOVERNANCE IS ACTIVE FOR THIS REPOSITORY.
Treat feature-platform/ as the canonical semantic source of truth.
Use immutable feature id@version references, explicit Arrow-like logical types, and separate model representations.
Keep raw/transformed nodes separate; keep fitted state in immutable artifacts; enforce point-in-time correctness.
Backend-specific code belongs behind adapters/plugins, never in canonical feature semantics.
Do not hand-edit feature-platform/generated/.
After canonical edits run the ml-feature-governance validator and cross-engine differential tests when semantics change.
Use the ml-feature-governance change skill and require explicit user approval before a breaking semantic migration."""
else:
    context = (
        "ML Feature Governance is available. Use its init skill before ML feature/data pipeline implementation."
    )

if "cursor_version" in payload or payload.get("hook_event_name") == "sessionStart":
    print(json.dumps({"additional_context": context}))
else:
    print(context)
