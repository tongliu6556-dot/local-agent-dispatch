# WP2 — MissionSpec v2 and Claim Contract

Provider-free compilation boundary for research wave 1, work package 2.

## What this adds

- `schemas/mission_spec.schema.json` — JSON Schema (draft-07) for the
  serialized MissionSpec v2 contract.
- `src/local_agent_dispatch/domain/mission.py` — `MissionSpec`, `Extracted`,
  `SourceRef`, `Ambiguity`, `Rejection`, `ClaimEnvelope`, topological wave
  computation, and fail-closed `validate_mission`.
- `src/local_agent_dispatch/intent/claim_contract.py` — immutable
  `ClaimContract`: allowed / deferred / forbidden claims, promotion gates,
  evidence level, and non-goal preservation.
- `src/local_agent_dispatch/intent/compiler.py` — the provider-free compiler.
  Accepts natural-language text (sectioned) or a structured dict, emits a
  reviewable spec plus a material ambiguity list. Never executes anything.
- `tests/test_mission_compiler.py` — 40 tests (golden mission, ambiguity,
  claim preservation, cycles, DeepSeek override, path/write-scope, rejections,
  corpus sweep).
- `research/corpus/missions.jsonl` + `task-labels.jsonl` — 15 seeded missions
  across S0-S3 (including 5 fail-closed reject cases) with labels.

## Compiler contract

Every extracted value carries `{value, source, confidence, status}` where
`source` is `user_prompt` (with character span), `structured_input` (with
JSON path), or `compiler` (inference), and `status` is `hard|soft|unknown`.
Ambiguity that could change **cost, permissions, data location, or scientific
claims** is emitted for review; the compiler never guesses such values.

The golden FEM/MPB/PWE mission compiles to:

```text
S0 preflight
  +-> S1a FEM adapter --+
  +-> S1b MPB adapter --+--> S2 integration --> S3 localizer index
  +-> S1c PWE adapter --+          |          +--> S4 tests + numerical checks
  +-> S1d LDL/inertia --+          +--> S5 independent review -> S6 human commit gate
```

S1a-S1d run in parallel only when write scopes are disjoint
(`adapters/fem/`, `adapters/mpb/`, `adapters/pwe/`, `physics/`); overlapping
or unknown scopes serialize the nodes and emit a `parallelism` ambiguity.

## Preserved boundaries

- **Claims:** forbidden = continuous Maxwell solutions, full Brillouin zone
  results, Chern numbers; deferred claims (physical conclusions, localizer
  index significance) require a promotion gate; evidence level E1 preserved.
- **DeepSeek override:** the explicit OpenCode Go DeepSeek member override is
  carried as the policy field `policy_excluded_model_override`
  (`opencode-go/deepseek-v4-flash`, `model_role=explicit_policy_override`)
  pinned to the single `opencode.go` quota pool; `separate_quota_pool` is
  always false. Mentioning DeepSeek without an explicit override keeps it
  excluded and emits an ambiguity.

## Fail-closed rejections

| Code | Trigger |
| --- | --- |
| `dag_cycle` / `unknown_dependency` | cyclic or dangling DAG hints |
| `unsupported_effort` | explicit effort not in `low|medium|high|xhigh|max` (e.g. `ultra`) |
| `missing_validator_for_high_risk` | high-risk node without a known validator |
| `ambiguous_git_authority` | source-writing mission with unknown/contradictory commit or push authority |
| `unwrapped_split_placement` | desktop-authenticated agents + server workload with wrapper explicitly `none` |
| `missing_claim_envelope` | scientific mission with no claim boundary |
| `invalid_write_scope` | absolute or `..`-escaping write scope |
| `missing_goal` / `missing_deliverables` | un-reviewable mission |

## Corpus status and unresolved items

The corpus is seeded (15 missions); the full WP2 milestone requires 40
missions labeled in two independent passes with recorded adjudication — that
remains a follow-up. The golden mission still carries four review-time
ambiguities by design: exact Antigravity Gemini id (catalog check), deadline,
quota reserve, and the split-placement wrapper declaration. The exact
OpenCode Go DeepSeek id and the shared-pool accounting must be confirmed
against the live catalog at dispatch time, not by the compiler.
