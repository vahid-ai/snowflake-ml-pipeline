---
name: symbol-navigation
description: Navigate definitions, references, implementations, types, diagnostics, and call hierarchies using an existing LSP, JetBrains MCP, or Serena MCP instead of reading whole files.
---

# Symbol navigation

Use this when symbol identity matters.

## Preference order

1. Existing native LSP/code-intelligence tool already active in the host.
2. JetBrains MCP if a JetBrains IDE already has a warm project index.
3. Serena MCP for portable symbol-level retrieval/editing.
4. Build/compiler/type-checker for final validation.

Use symbol overview/listing before retrieving bodies. Retrieve only the body of the symbol needed for the task. Use find-references/implementations instead of grepping when overloading, inheritance, aliases, or generated imports make text matching ambiguous.

### JetBrains MCP

Best when IntelliJ/Android Studio/PyCharm already understands the project model. Prefer IDE symbol search, symbol info, file problems/diagnostics, project dependencies, run configurations, and debugger operations over duplicate indexing.

### Serena MCP

Best portable choice when no warm IDE index exists. Prefer symbol listing → targeted symbol body → references. Avoid asking Serena for entire files if symbol APIs can answer.

### LSP limitations

LSP is not a substitute for semantic concept search. If the symbol name is unknown, try `rg` guesses and then GrepAI. If transitive blast radius across many modules is the goal, escalate to a graph tool.
