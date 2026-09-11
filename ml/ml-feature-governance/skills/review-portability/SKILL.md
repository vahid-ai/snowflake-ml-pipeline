---
name: review-portability
description: Independently review ML feature implementations for cross-engine typing, determinism, capability, and semantic-equivalence risks.
---

# Cross-Backend Portability Review

Review without modifying files. Check for ambiguous types, signed/unsigned loss, float precision drift, timestamp and
timezone drift, null behavior, runtime-default hashes, UTF-8 and regex differences, window boundary/order differences,
non-deterministic partition ordering, vector shape mismatch, hidden fallbacks, and backend code in canonical definitions.

Require explicit capability mappings, fail-fast behavior for unsupported semantics, a declared equivalence policy, and
golden differential tests for every affected supported backend. Return findings ordered by severity with concrete fixes.
