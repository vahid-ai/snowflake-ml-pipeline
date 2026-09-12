# Change contracts

Create a versioned YAML change contract here for semantic, backend-support, or migration work. The contract is the
compact audit record for intent, compatibility, affected consumers, implementation order, verification, and approval.

Example:

```yaml
spec_version: "1.0"
changes:
  - id: device.add_network_windows
    version: 1
    status: proposed
    change_type: additive_semantic
    intent: Add trailing 30 minute and 1 hour network-volume features.
    compatibility: backward_compatible
    affected_features:
      - device.bytes_sent_5m@1
    proposed_features:
      - device.bytes_sent_30m@1
      - device.bytes_sent_1h@1
    affected_consumers: []
    verification:
      required:
        - contract
        - dag
        - leakage
        - portability
        - golden_differential
    approval:
      required: false
```

Use `compatibility: breaking` only with a non-empty `migration.strategy`, a consumer inventory, and
`approval.required: true`. Explicit user approval is still required before implementation or rollout; setting a YAML
field does not grant approval.
