# Dispatch Schema Contract

This document outlines the schema validation strategy for durable objects in local-agent-dispatch (`task_packet`, `dispatch_plan`, `runtime_state`, `event`).

## Versioning and Unknown Values

All durable objects MUST contain a `schema_version` integer (currently 1). Modern
planner packets additionally require `packet_id`, `job_id`, `write_scope`,
`validation_required=true`, non-empty `required_artifacts`, and a non-empty
`attempts` list with exact `attempt_id`/`adapter`/`transport`/`model` fields.
Unknown extension fields remain allowed, but malformed known fields fail closed.
Older hand-written queues may be read only when the packet explicitly sets
`legacy_compatibility=true`; the controller records that evidence level rather
than silently upgrading it to a modern packet.

Model-policy exceptions are task-scoped extension fields. For example,
`allow_policy_excluded_models` may name one exact model that the user has
explicitly authorized, together with `model_by_pool`; the planner still checks
that the model is visible in the current catalog and charges the existing
shared pool. An exception never changes quota accounting or bypasses
validation, write-scope, or host gates.

## Evidence Fields

Evidence fields are explicitly captured and structured (e.g. `pools` inside a `runtime_state`, or nested `attempt` maps) because the dispatch planner relies on their shapes to match capabilities against requirements. By explicitly validating that these fields are valid dictionaries if present, we protect the planner from type errors without demanding a rigid schema for the rest of the payload.

Secret-like fields (such as `token`, `password`, `api_key`) are globally banned
in public snapshots and task packets. The focused validator scans keys across
the entire structure to prevent accidental credential leakage in telemetry,
queue state, or open snapshots.

## Future Migrations

When migrating to a new schema version (e.g. `schema_version = 2`):

1. **Bump Version:** The objects written by the prototype will emit `schema_version: 2`.
2. **Backwards Compatibility:** The schema validator `scripts/dispatch_schema.py` should be updated to accept `schema_version: 1` and `2`. It must apply structural rules depending on the version. 
3. **Rollout Strategy:** Update consumers (planner/controller) to handle version 1 and 2 gracefully, or transparently upgrade version 1 objects to version 2 in memory before applying logic.
4. **Deprecation:** Once no version 1 objects exist in the system (e.g. active jobs complete), the support for version 1 can be phased out.
