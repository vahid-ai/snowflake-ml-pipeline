---
name: learn-from-failure
description: Convert a debugging or tool failure into a durable lesson only after root cause and fix are verified, deduplicating against existing memory and recording evidence instead of speculative hypotheses.
---

# Learn from failure

Use after a meaningful failure has been fixed and verified.

## Promotion gate

Do not save a durable lesson until all are true:

1. **Failure** — state the observable failure/error.
2. **Cause** — identify the root cause with evidence, not just a guess.
3. **Fix** — state the minimal change that resolved it.
4. **Verification** — cite the command/test/build/behavior proving the fix.
5. **Recurrence value** — explain why this is likely useful again.
6. **Deduplication** — search existing memory for an equivalent lesson and update rather than duplicate it.

## Preferred memory record

Store a short record with fields conceptually equivalent to:

- title;
- scope/repository/component;
- symptom;
- root cause;
- verified fix;
- verification evidence;
- date or version context;
- tags.

Never store credentials or raw secret-bearing logs. Redact tokens, keys, cookies, Authorization headers, private URLs, and personal data.

If the cause is uncertain, keep it in the current session only and do not promote it.

If `AGENT_TOOLKIT_CAPTURE_FAILURES=1` is enabled, the plugin may have recent sanitized failure events in its writable plugin data directory. These are leads, not truths.
