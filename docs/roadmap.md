# Product roadmap and current boundary

Resource-bearing SQLite packets now use fenced schema-v3 reservations; legacy JSON history is audited by `lad legacy-import` and the read-only L0 view is available through `lad cockpit`.

The evidence-gated master research program is documented in
[`docs/superpowers/plans/2026-08-12-local-agent-dispatch-research-program.md`](superpowers/plans/2026-08-12-local-agent-dispatch-research-program.md).

This is the implementation roadmap for the open-source control plane. “Done”
means provider-free evidence exists in this checkout; it does not mean a real
provider, GPU runtime, or SSH workload is currently available.

| Phase | Current state | Remaining acceptance gate |
| --- | --- | --- |
| P0 — package/spec | Partial: installable `lad`, versioned schemas, CI, security/release docs | User-approved license, GitHub repository, secret/path scrub, clean-clone release audit |
| P1 — durable control plane | Partial: SQLite WAL leases/claims/completions/recovery, fenced replan requeue, persisted `retry_at_utc` exponential backoff, strict local PID-liveness recovery gate (`alive/unknown` → blocked), `auto` SQLite-first selection with explicit JSON legacy detection, packet validation, schema-v3 fenced resource reservations, claim-time governor admission for resource-bearing packets, metadata-only legacy-history importer, and L0 Mission Cockpit projection | Unify every mutation in one transaction, prove crash/restart and live enqueue semantics across migration paths, add remote PID/process-group handoff evidence before stale reclaim, and exercise reservation heartbeat/release under fault injection |
| P2 — adapters/OS | Partial: system-first scanner plus Codex/OpenCode Go/Cursor/Antigravity/server-local boundaries; stdlib-only five-kind plugin protocols, explicit registry, provider-free conformance fixtures, and search-first Evidence Discovery/Compatibility Resolver (`lad evidence`) | Migrate existing provider/runtime/transport/probe scripts behind the protocol and resolver, hosted macOS/Windows CI, and no external-skill dependency |
| P3 — dynamic planning | Mostly implemented: provider-free `lad capture` TaskPacket, versioned capture schema, planner-compatible safe-ID DAG `planner_jobs`, inferred/explicit parallel waves, per-node P50/P90 estimates, exact task-family/model/host EWMA calibration with bias evidence, token/USD evidence, shared pools, rolling horizon, writable-mount selection, load/disk/quota gates, capture model allowlists and stale-estimate clearing; approved-wave `lad closed-loop` now exercises SQLite→monitor→replan→read-only next-plan | Reset-window/fairness policy, automatic replan for real provider/remote observations, confidence/complexity guardrails, broader task fixtures |
| P4 — server continuity | Partial: SSH capacity/data-route probes, server-local smoke matcher, durable worker spool with fenced recovery, artifact freshness, read-only artifact observation, bounded fake executor/resume handoff, redacted SSH stdin transport client, loopback OpenAI-compatible SSH runtime with remote artifact/validator fencing, direct-server download policy | Persistent remote worker/service, worktree/data sync, CUDA/runtime fit, egress/billing stop evidence, offline continuation after local chat loss |
| P5 — monitoring/safety | Partial: 180-second observer, placement-aware feedback, exact model/variant runtime overlay, SQLite snapshot→monitor adapter with lane/lease/heartbeat evidence, provider-free Resource Governor v1 for RSS/swap pressure and local-lane admission, and controller-linked reservation evidence | Integrate OS-native pressure/true hysteresis, owned-process pause/resume, streaming logs and PID registration for all adapters, validator/hash completion proof everywhere, path/secret/chaos/fault-injection matrix |
| P6 — public alpha | `v0.1.0-alpha.3` published at [GitHub](https://github.com/tongliu6556-dot/local-agent-dispatch); Linux CI, Apache-2.0, clean-tree scrub, offline demo, and server-side OpenCode Go canary verified | macOS/Windows hosted CI, public fake-provider E2E, persistent remote worker/service, and the remaining provider/remote-runtime gates |

## Current operating rule

The safe path is:

```text
local system scan
  → provider/SSH preflight
  → task P50/P90 estimate + hardware/data fit
  → rolling-horizon model/pool/host plan
  → reviewed packet bridge
  → auto (SQLite-first; existing JSON runs preserved) or explicit JSON controller
  → 180-second monitor
  → provider-vs-compute replan
```

Unknown quota or resource evidence is never fabricated. A missing PID/log
breadcrumb is `unknown`, not healthy. Desktop-authenticated providers remain on
the local execution host; a remote workload requires an explicit wrapper and
separate telemetry. Large downloads and full workloads remain server-first.

`lad legacy-import --root PATH` audits old JSON runs without changing them.
Adding `--output-db PATH` creates a new sanitized SQLite metadata copy only;
prompts, argv, logs, credentials, and artifacts are never imported. Resource-
bearing planner packets receive a fenced reservation before a strict SQLite
CLI claim. `lad cockpit --snapshot PATH` produces the read-only L0 Mission
Cockpit; detailed events and validators remain the L3 audit layer.
