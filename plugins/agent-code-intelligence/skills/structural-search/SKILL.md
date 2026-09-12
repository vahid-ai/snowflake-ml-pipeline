---
name: structural-search
description: Use ast-grep for syntax-aware code search and rewrites when the target is a program structure, invocation shape, declaration pattern, or codemod rather than plain text.
---

# Structural search with ast-grep

Use `sg` / `ast-grep` after exact search when regex would be brittle.

Examples:

```bash
sg -p 'console.log($A)' -l ts
sg -p '$A && $A()' -l ts -r '$A?.()'
```

Prefer structural search for:

- finding function calls with specific argument shapes;
- API migrations and codemods;
- declarations with particular modifiers/annotations;
- patterns where comments/strings would create regex false positives;
- safe syntax-aware rewrites.

Use `rg` instead when the target is simply a literal string or filename. Use LSP instead when the task is about symbol identity, references, or types rather than syntax shape.

For unfamiliar ast-grep pattern syntax, inspect `sg --help` or upstream docs before inventing a pattern.
