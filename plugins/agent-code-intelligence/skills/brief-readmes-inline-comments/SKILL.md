---
name: brief-readmes-inline-comments
description: Keep README files brief and explain implementation beside the code when adding or updating source documentation, onboarding instructions, or READMEs.
---

# Brief READMEs, useful inline comments

Use README files as entry points: a short purpose statement, essential setup/run/test
commands, and links to code or focused operational documentation. Keep them easy to
scan. Avoid implementation walkthroughs, function inventories, duplicated CLI help,
and accumulating a new section for every change.

Put implementation explanations beside the code they describe:

- Explain a module's responsibility, a function's contract, and the purpose of
  substantial processing stages when names and types do not make them clear.
- Describe why a choice exists, data flow, units, invariants, side effects, failure
  handling, and non-obvious library constraints. Avoid narrating obvious syntax.
- Use a short comment near the relevant statement or block. Use docstrings when
  callers need API behavior, input/output expectations, or important constraints.
- Read the implementation before describing it. Do not claim guarantees or
  behavior the code does not provide. Update nearby comments when behavior changes.
- Preserve existing accurate explanations; remove stale or duplicated prose rather
  than layering a second explanation on top.

Keep user-facing prerequisites, credential setup, destructive-operation warnings,
and required usage instructions discoverable outside implementation details. Link
to focused docs for material that cannot reasonably live in a comment. Do not hide
necessary operating instructions just to shorten a README.

For documentation-only work, preserve behavior and avoid unrelated refactors.
Check the diff for executable changes, validate any edited examples, and run
relevant existing checks. Respect generated/managed-file workflows and checksums.
An explicit user request for detailed documentation takes precedence over brevity.
