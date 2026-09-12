---
name: exact-search-cli
description: Use fast deterministic CLI tools ripgrep, fd, jq, yq, and optionally fzf to locate exact code, files, errors, configuration, imports, or structured-data fields with minimal context.
---

# Exact search CLI

Use this before semantic search for literal or predictable identifiers.

## Preferred commands

```bash
fd 'auth' .
fd -e kt -e java .
rg -n 'refreshToken|refresh_token' src test
rg -l 'AuthenticationException' .
rg -n --glob '*.kt' 'TODO|FIXME' .
```

For structured files, reduce them before inspection:

```bash
jq '.scripts, .dependencies' package.json
yq '.services | keys' docker-compose.yml
```

Use `rg --files` or `fd` rather than `find` unless POSIX portability is required. Respect `.gitignore` unless the task explicitly targets ignored/generated files.

Do not dump lockfiles or API payloads into context when `jq`/`yq` can select the required fields.
