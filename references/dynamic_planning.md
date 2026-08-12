# Dynamic Planning and Feedback Loop

## Contents

1. Usage signals
2. Cost-aware planning
3. Rolling-horizon procedure
4. State and job schemas
5. Monitoring decisions

## Usage signals

Start with `local_system_scan.py`. Provider discovery is conditional on its
installed-CLI inventory, and compute planning must use its observed local
CPU/RAM/disk/GPU facts rather than a synthetic fallback host. This stage is
local-only and does not query authentication, catalogs, quota, or models.

Use `codex /usage` for a human-readable account token-activity view. On the
locally verified Codex CLI 0.146.0, this view shows lifetime/recent token
activity; it is historical usage, not remaining scheduling capacity.

Use the machine-readable helper for scheduling:

```bash
python3 $HOME/.codex/skills/local-agent-dispatch/scripts/codex_usage_snapshot.py \
  --output codex-usage.json
```

It reads `account/rateLimits/read` and `account/usage/read` through Codex's
local app-server without sending a model prompt. Treat rate-limit windows as
capacity and token activity as trend evidence only.

Map live rate-limit buckets by returned identity:

- `codex` -> `codex.luna` for the approved Luna/max lane.
- `codex_bengalfox` or a live limit name containing Spark -> `codex.spark`.
- Preserve unknown limit IDs as separate pools rather than guessing.

Refresh Cursor catalog/runtime evidence and Antigravity `/usage` using the
existing skill rules. Do not paid-probe a model merely to refresh health.

For OpenCode Go, use `opencode_go_snapshot.py`. Treat all exact
`opencode-go/*` members as one `opencode.go` subscription pool. Its local
`stats` output is historical usage, not remaining allowance; keep five-hour,
weekly, monthly, and overage-fallback state unknown when the CLI returns no
machine-readable balance. Catalog/auth evidence creates a candidate, while the
first authorized task supplies runtime acceptance or exact rejection evidence.

For a single persisted snapshot, run `dispatch_preflight_scan.py`. It also
discovers server-local APIs and overlays exact runtime rejections on live
catalog visibility before the planner builds candidates.

## Cost-aware planning

Optimize useful work per quota unit, not remaining percentage alone:

```text
utility =
  priority_value
  + task_and_difficulty_fit
  + quality_value
  + latency_value
  + user_primary_model_bonus
  + estimated_work_per_quota_unit
  - failure_and_stall_penalties
  - reserve_violation_penalty
```

Estimate quota cost as:

```text
estimated_quota_percent =
  estimated_minutes
  * measured_quota_percent_per_minute
  * difficulty_multiplier
```

Prefer an explicit per-job/per-pool estimate when supplied. Otherwise use the
latest observed pool/model rate. Fall back to a prior only when no observation
exists.

Within a multi-model pool, an attributable exact-model rate takes precedence
over a pool-wide rate. Otherwise apply only a live advertised usage multiplier
to the pool prior. Preserve OpenCode's per-million-token catalog costs as price
metadata, but do not pretend they are a measured remaining-quota percentage.
When a task also contains explicit or measured `input_tokens` and
`output_tokens` P50 bounds, the planner emits `estimated_usd_cost` and
`cost_evidence=model_price_and_token_hints` on the assignment. Missing price or
token evidence remains `unknown`; it is never represented as zero cost.

Use `0.0125 displayed quota percent/minute` as the initial Luna/max prior. This
is a user-observed heuristic derived from a roughly 40-minute Luna/max run with
less than one displayed percentage point of movement; it is not a guaranteed
price. A zero displayed delta creates an upper bound because the UI/API reports
integer percentages. Keep the lower prior when the observation is too short to
improve it.

Do not infer a model-specific rate when multiple or unknown external consumers
may use the same pool. Record that sample as pool-level/confounded. Update a
model rate only when the state or sole worker explicitly marks the observation
`exclusive_pool_observation=true`. Use an EWMA for positive deltas so one burst
does not dominate future plans.

## Rolling-horizon procedure

Use model-predictive control: plan a small horizon, execute only the first wave,
observe, then solve again from the new state.

1. Capture the request into a bounded TaskPacket.  This normalizes an explicit
   or conservative inferred DAG, records parallel waves, and optionally
   calibrates an exact task-family/model/host history bucket:

   ```bash
   python3 $HOME/.codex/skills/local-agent-dispatch/scripts/task_capture.py \
     --task task.json --repo-root . --history observations.json \
     --model codex.spark --host remote-a
   ```

   The capture boundary reads file metadata only; it does not execute a
   project command or send a provider prompt.  Missing observations remain
   `unknown`; invalid dependencies and cycles produce `dag_invalid`.
2. Run the local system scan, then build the dependency graph and bounded job records.
3. Refresh only installed providers plus runtime-health and compute-host signals.
4. Estimate resource requests/headroom and run the joint pool/host planner over
   the next 6-10 ready jobs.
5. Dispatch only the selected first wave with disjoint write scopes.
6. Monitor for 180 seconds by default, polling every 30 seconds.
7. Feed progress, artifacts, failures, quota deltas, and host pressure back into state.
8. Replan immediately on completion, failure, stall, quota change, model
   rejection, host/data-route pressure, or user scope change; otherwise replan
   after the monitor window.

Planning command:

```bash
python3 $HOME/.codex/skills/local-agent-dispatch/scripts/dynamic_dispatch_planner.py \
  --state state.json --jobs jobs.json --max-lanes 4 --horizon 8 \
  --output plan.json
```

Monitoring command:

```bash
python3 $HOME/.codex/skills/local-agent-dispatch/scripts/dispatch_monitor.py \
  --state state.json --duration-seconds 180 --interval-seconds 30 \
  --stall-seconds 120 --state-out state.after-monitor.json \
  --report monitor-report.json
```

Run the planner again with `state.after-monitor.json`. The monitor does not kill
or reroute processes automatically; it emits `keep_and_monitor`,
`replan_unblocked_jobs`, `reroute_or_pause`, or `replan` for the supervising
agent to apply safely.

For SQLite-backed runs, use `lad monitor-state --db dispatch.sqlite3` to create
the monitor state from durable jobs/attempts/leases. This adapter is read-only
and marks running attempts without explicit PID/log breadcrumbs as `unknown`.

For a launcher that exits after starting a durable service, monitor the service
PID file rather than the launcher PID. Set `pid_path` to the service PID file.
For an append-only log reused across retries, set `log_attempt_marker` to the
unique marker written at the beginning of the current attempt. Error
classification then ignores stale failures from earlier attempts.

## State and job schemas

Minimum pool state:

```json
{
  "pools": {
    "codex.luna": {
      "health": "balanced",
      "effective_remaining_percent": 28,
      "quota_rate_percent_per_minute": 0.0125,
      "reserve_percent": 20,
      "max_concurrency": 1,
      "inflight": 0,
      "recent_failures": []
    }
  },
  "workers": [],
  "completed_jobs": [],
  "failed_jobs": [],
  "monitor_seconds": 180,
  "poll_interval_seconds": 30
}
```

Minimum job record:

```json
{
  "job_id": "J1",
  "task_type": "audit",
  "difficulty": 4,
  "priority": "high",
  "latency_priority": "normal",
  "estimated_minutes": 40,
  "depends_on": [],
  "write_scope": "worker_J1/",
  "required_artifact": "worker_J1/status.md"
}
```

Optional job overrides include `allowed_pools`, `excluded_pools`,
`preferred_pools`, `avoid_providers`, `estimated_quota_cost`,
`quota_cost_by_pool`, `allow_reserve`, `allow_server_local`,
`allow_unreviewed_server_local`, and `high_stakes`. A server-local pool is bound
to the host that serves its model and is not eligible for high-stakes/audit work
unless explicitly allowed; medium/hard outputs require later provider review.
When a server-local agentic smoke publishes `max_difficulty` and
`requires_provider_review`, those calibrated values are hard scheduler gates;
catalog/API visibility alone never makes the pool ready.

An `opencode.go` pool also carries `catalog_models`, `role_model_candidates`,
`available_model_variants`, `rejected_models`,
`rejected_model_variants`, `model_usage_multipliers`, and
`overage_fallback_state`. These fields choose an exact model within one shared
capacity counter; they never create per-model quota.

## Monitoring decisions

- Growing logs or artifacts -> keep the worker and preserve its route.
- Completed required artifact plus exited process -> mark complete and unlock
  dependencies before replanning.
- No progress past the stall window -> inspect once, then reroute or pause.
- Explicit quota/rate failure -> cool the shared pool.
- Capability/model rejection -> reject only the exact model/variant tuple.
- Quota/auth/network failures -> update the shared pool; do not create a
  permanent exact-model rejection.
- Provider auth/network failure -> degrade the provider pool and prefer an
  independent healthy backend.
- Quota delta with one attributable model -> update that model's cost-rate EWMA.
- Quota delta with concurrent consumers -> update only pool-level cost evidence.
