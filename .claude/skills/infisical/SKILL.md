---
name: infisical
description: >
  Safely read, list, and use secrets from Infisical (CLI, machine identities,
  infisical run). Use this skill whenever the task involves Infisical in any
  way — listing or checking which secrets exist in a vault or project,
  authenticating a machine identity, injecting secrets into a command with
  `infisical run`, or debugging why a secret "isn't there". Especially use it
  before parsing any `infisical secrets` output: the CLI's table format
  silently loses rows under naive parsing, and this skill exists because that
  exact bug caused a false "secret is missing" diagnosis.
---

# Working with Infisical safely

## The core pitfall: never parse the CLI table

`infisical secrets` prints a box-drawing table (`│`-delimited, with `─` rules).
Two things make it hostile to scripting:

1. **Wide rows wrap or reflow.** The row with the longest name + value (often
   exactly the secret you care about, like a 64-char key) can wrap across
   lines. Whitespace-based parsing (`awk '{print $2}'`) then drops or mangles
   that row — *silently*. The result looks like a complete listing with one
   secret missing, which sends the investigation in exactly the wrong
   direction ("did you save it as a personal override?") when nothing is
   wrong with the vault.
2. **The table contains secret VALUES.** Any pipeline that captures the table
   risks echoing values into logs and transcripts.

So: **never scrape the table.** Get names from JSON instead:

```bash
python .claude/skills/infisical/scripts/list_secret_names.py            # everything visible
python .claude/skills/infisical/scripts/list_secret_names.py --env dev  # one environment
```

The script authenticates from `INFISICAL_TOKEN` or
`INFISICAL_CLIENT_ID`/`INFISICAL_CLIENT_SECRET`, walks every visible project
and environment recursively (folders included), and prints only names and
counts — never values.

If the script is unavailable, the same principle applies: query the REST API
(`GET /api/v3/secrets/raw?workspaceId=..&environment=..&recursive=true`) and
extract `secretKey` fields with a JSON parser. As a last resort on the CLI
table, split on the `│` delimiter, never on whitespace — but prefer JSON.

## Always cross-check the count

Independent sources for "how many secrets are there" that don't share the
table's failure mode:

- `infisical run` prints `Injecting N Infisical secrets into your application
  process` — this N is authoritative for what a process actually receives.
- The JSON API's array length.

If a listing you produced disagrees with either number, your listing is wrong
— not the vault. Reconcile before telling anyone a secret is missing.

## Secret-value hygiene

- Print names and counts, never values. Don't `cat`, `echo`, or log a value,
  and don't run `infisical secrets` / `infisical export` bare in a way that
  lands values in the transcript.
- To *use* secrets, inject them: `infisical run --projectId=<id> --env=<slug>
  -- <command>`. The values exist only in that child process's environment.
- If a value must be checked (e.g. "is this the right key?"), compare hashes
  or test it against the target service inside a subprocess, and report only
  the verdict.

## Machine-identity auth, quickly

```bash
TOKEN=$(infisical login --method=universal-auth \
  --client-id="$INFISICAL_CLIENT_ID" --client-secret="$INFISICAL_CLIENT_SECRET" \
  --plain --silent)
export INFISICAL_TOKEN="$TOKEN"
```

On `401 Invalid credentials`, check in this order:
1. Wrong instance — try `--domain https://eu.infisical.com` (or the org's
   self-hosted URL). All cloud regions return the same 401 text.
2. The client ID is the identity's **Universal Auth client ID**, not the
   identity ID (both are UUIDs; they are different).
3. The client secret was regenerated, expired (TTL), or hit its max-use cap.
4. The identity uses a different auth method entirely (Token Auth, OIDC).

## What a machine identity can and cannot see

Listings reflect the identity's access, not the whole vault:

- Only projects the identity was granted appear in `GET /api/v1/workspace`.
- Secrets saved as **personal overrides** are invisible to machine
  identities; only **shared** secrets appear.
- A scoped role can hide paths or environments.

So "the identity can't see it" and "it isn't in the vault" are different
claims — but before reaching for either, rule out your own parsing (see the
count cross-check above). State which claim the evidence actually supports.
