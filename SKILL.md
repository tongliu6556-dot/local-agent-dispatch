---
name: local-agent-dispatch
description: "Scan the local system first, then dynamically route work across Codex CLI, OpenCode Go, Cursor Agent, Antigravity, local model runtimes, and directly reachable compute servers. Use for local agents, GPT-5.6 Luna, GPT-5.3 Codex Spark, OpenCode Go, Cursor/Composer/Grok, Antigravity/Gemini, usage/quota, background dispatch, multi-model scheduling, resource estimates, data/environment downloads, CPU/GPU/RAM/VRAM/disk planning, remote SSH execution, or minute-scale monitoring."
---

# Local Agent Dispatch

Use this skill to turn a normal request into an explicit, observable, quota-aware
dispatch across local provider CLIs and server-local model backends:

- Codex CLI
- OpenCode Go through the locally authenticated OpenCode CLI
- Cursor Agent
- Antigravity CLI
- SSH-hosted vLLM, Ollama, or llama.cpp, optionally through a tool-capable agent

The scheduler chooses a pool dynamically from the live model catalog, task
difficulty, shared quota state, recent failures, expected latency, and the
user’s current preferences.

Treat model selection and compute placement as separate but jointly optimized
decisions: the agent CLI may run locally while the workload executes on a
verified SSH host.

## Hard Scope Boundaries

This skill must not invoke standalone Claude Code/`claude` or standalone
DeepSeek backends. Models exposed inside Cursor Agent or Antigravity remain
eligible and must be charged to their host CLI's shared quota pool; a model
brand does not create a separate backend or quota pool. Also do not dispatch
any unapproved Codex GPT-5.6 family member other than Luna. Keep DeepSeek
members returned by the OpenCode Go catalog visible for accounting, but exclude
them from dispatch policy unless the user explicitly changes this boundary.

## User-Preferred Codex CLI Models

The two primary Codex CLI lanes are:

1. `gpt-5.6-luna` with `model_reasoning_effort="max"` for normal, hard, and
   high-value work.
2. `gpt-5.3-codex-spark` with `model_reasoning_effort="xhigh"` for small,
   bounded, latency-sensitive coding loops when the live cache advertises it.

The live 2026-08-01 cache contains `gpt-5.6-luna`, not a model slug named
`gpt-5.5-luna`. Normalize casual "5.5 Luna max" wording to the real
`gpt-5.6-luna` + `max` pair and tell the user when that normalization matters.

Luna currently supports `low`, `medium`, `high`, `xhigh`, and `max`. It does
not advertise `ultra`. Normalize a casual "Luna ultra" request to Luna/max;
fail closed if an explicit unsupported model/effort pair is requested.

Run the preflight before constructing a Codex command:

```bash
python3 $HOME/.codex/skills/local-agent-dispatch/scripts/codex_model_preflight.py \
  --preset luna-max
```

or:

```bash
python3 $HOME/.codex/skills/local-agent-dispatch/scripts/codex_model_preflight.py \
  --preset spark-fast
```

Direct command shapes:

```bash
codex exec -m gpt-5.6-luna \
  -c 'model_reasoning_effort="max"' \
  -c 'approval_policy="never"' \
  -C "<trusted-workspace>" --sandbox workspace-write "<task>"
```

```bash
codex exec -m gpt-5.3-codex-spark \
  -c 'model_reasoning_effort="xhigh"' \
  -c 'approval_policy="never"' \
  -C "<trusted-workspace>" --sandbox workspace-write "<bounded-code-task>"
```

Current Codex CLI no longer accepts the old `--ask-for-approval never` option.

## Codex Usage Signals

Codex CLI 0.146.0 supports interactive `/usage`, but its visible panel is
historical token activity rather than a reliable remaining-quota percentage.
For scheduling, read the machine-facing rate-limit buckets without sending a
model prompt:

```bash
python3 $HOME/.codex/skills/local-agent-dispatch/scripts/codex_usage_snapshot.py
```

Treat the returned `codex` bucket as `codex.luna` and the live Spark-named
bucket (`codex_bengalfox` in the current response) as `codex.spark`. Preserve
unknown limit IDs separately. Do not merge Luna and Spark capacity.

## System-First Discovery

Before provider, quota, or remote probes, run the privacy-preserving local
inventory:

```bash
python3 $HOME/.codex/skills/local-agent-dispatch/scripts/local_system_scan.py \
  --workspace "<trusted-workspace>"
```

This stage detects the OS, architecture, CPU, available/total RAM, workspace
and cache disk, GPU/accelerators, installed agent/runtime CLIs, and only the
canonical names and PIDs of active agent/model processes. It does not query
credentials, provider catalogs, quotas, or model endpoints; it does not send a
model prompt; and it never stores process arguments. It supports macOS, Linux,
and Windows with platform-specific probes and explicit unknown fields when a
signal is unavailable.

`dispatch_preflight_scan.py` enforces this order: local system first, selected
provider/compute discovery second, pool construction plus runtime overlays
third. Missing optional CLIs disable only their own probes. Never invent a
synthetic host when the system scan and compute inventory provide no real host.

The local capacity gate is hard: if either the workspace or dispatch-cache
volume has less than 20 GiB or 10 percent free, set
`local_bulk_allowed=false`. Small source edits and bounded tests may continue,
but datasets, models, environments, caches, and bulk output move to a verified
server.

For a public repository, commit only source, tests, templates, schemas, and
sanitized examples. Never commit `hosts.json`, preflight/runtime snapshots,
provider account details, PIDs, logs, task packets, local paths, SSH endpoints,
or artifacts. Runtime state defaults to
`$HOME/.codex/local-agent-dispatch` and can be relocated with
`LOCAL_AGENT_DISPATCH_HOME`; private inventories remain outside the repository.

## Hardware-to-Server Fit

After the local scan and before planning a workload, turn the saved preflight
facts into an explicit placement report:

```bash
python3 $HOME/.codex/skills/local-agent-dispatch/scripts/hardware_fit_planner.py \
  --preflight $HOME/.codex/local-agent-dispatch/preflight-state.json \
  --jobs "<jobs.json>" \
  --workspace "<trusted-workspace>" \
  --output $HOME/.codex/local-agent-dispatch/hardware-fit-report.json
```

This command is read-only. It does not probe a host, install a runtime, download
a model, or send a provider prompt. It reports the actual local CPU/RAM/GPU/
disk/OS/architecture, each job's P50-like bounded resource request and
server-first reasons, the minimum server configuration, eligible SSH hosts,
rejection reasons, and a decision of `run_local`, `run_server`, `pilot_first`,
or `blocked`.

Use the report as a hard gate, not as runtime readiness. A server candidate still
needs live SSH reachability, writable project path, runtime/model fit, direct
bulk-route evidence, and an agentic smoke before it becomes ready. Keep
`execution_host` and `workload_host` separate. If local disk is below 20 GiB or
10 percent free, local bulk work remains forbidden even when the local CPU/GPU
would otherwise fit.

For a packaged CLI, the same report is available as:

```bash
lad fit --preflight "<preflight.json>" --jobs "<jobs.json>" \
  --workspace "<trusted-workspace>"
```

For a single provider-free planning preflight, use the unified dispatch
surface with a saved snapshot:

```bash
lad dispatch --workspace "<trusted-workspace>" \
  --jobs "<jobs.json>" \
  --preflight "$HOME/.codex/local-agent-dispatch/preflight-state.json" \
  --max-lanes 4 --horizon 8 \
  --output "$HOME/.codex/local-agent-dispatch/dispatch-report.json"
```

`lad dispatch` preserves the required order—local system scan, preflight
merge, task P50/P90 estimate, local/remote hardware fit, then planner—and
returns a versioned read-only report. The report records exact host/model/pool
assignments, server-first reasons, unknown-resource and pilot gates, quota
uncertainty, and the planned parallel lanes. It never starts a provider or
sends a model prompt. Add `--live-probes --inventory PATH` only when fresh
catalog/quota/SSH discovery is explicitly wanted; that mode still has no model
execution or prompt.

The packaged equivalent for a complete local-first discovery is:

```bash
lad preflight --workspace "<trusted-workspace>" \
  --inventory "$HOME/.codex/local-agent-dispatch/hosts.json" \
  --output "$HOME/.codex/local-agent-dispatch/preflight-state.json"
```

`lad preflight` runs the local system stage synchronously before provider and
SSH probes. It preserves credentials only for this explicit discovery command,
never sends a model prompt, and writes the resulting snapshot under the private
runtime directory. Add `--skip-antigravity-usage` when the interactive
`/usage` TUI is unavailable; Antigravity quota then remains `unknown` and the
planner applies its normal hard gate or bounded-pilot policy.

## Planner-to-controller boundary

`dynamic_dispatch_planner.py` produces placement decisions, not executable
provider commands. Before any background run, convert an approved assignment
through the read-only bridge:

```bash
python3 $HOME/.codex/skills/local-agent-dispatch/scripts/plan_packet_bridge.py \
  --plan "<dispatch-plan.json>" --jobs "<jobs.json>" \
  --state "<state-or-hosts.json>" --adapters "<adapter-registry.json>"
```

The bridge preserves the exact model/variant, requires an explicit adapter
contract, validation command, write scope, prompt/result/artifact paths, and
rejects path traversal, missing adapters, and desktop-authenticated CLI plus
remote-workload split placement unless a declared wrapper exists. It is
`dry-run` by default and never starts a provider. A packet is enqueueable only
after this report is reviewed and the controller's run-level lease is held.

For the packaged command surface, the corresponding control loop is:

```bash
lad plan --state "<planner-state.json>" --jobs "<jobs.json>"
lad bridge --plan "<dispatch-plan.json>" --jobs "<jobs.json>" \
  --state "<preflight-state.json>" --adapters "<adapter-registry.json>"
lad run --run-dir "<run-dir>" --once
lad monitor --state "<monitor-state.json>" --duration-seconds 180
lad replan --monitor-report "<monitor-report.json>" --jobs "<jobs.json>"
lad resume --run-dir "<run-dir>"
```

For the SQLite backend, project its durable snapshot before monitoring:

```bash
lad monitor-state --db "<dispatch.sqlite3>" > "<monitor-state.json>"
```

This read-only adapter preserves exact placement, validation, artifact-hash,
lane, lease/fence, heartbeat, and explicit PID/log evidence. A running
attempt without a PID/log breadcrumb is reported as `unknown`; it is never
inferred to be healthy or complete.

For a queue that must survive the originating chat/session, start the
transactional worker as an independent process after reviewing and enqueuing
the approved packets. New queues may use `--backend auto` (the default),
which selects a workspace-local SQLite database; an existing `state.json`
run is kept on the JSON migration path. `status`/`resume` remain read-only
before initialization and report `status=not_initialized` without creating
an empty database:

```bash
lad run --backend sqlite --db "<dispatch.sqlite3>" \
  --workspace "<trusted-workspace>" --max-lanes 2 --detach
```

The command returns a PID metadata path and a private controller log. The
default `--max-idle-rounds 0` keeps the worker alive while the queue is empty;
it still executes only already-authorized packets and never infers new chat
intent. Inspect `lad status --backend sqlite --db ...` and use the recorded
PID/log when the chat quota is unavailable.

For an explicit transactional queue, use the same surface with
`--backend sqlite --db "<dispatch.sqlite3>"`. The SQLite path enforces WAL
transactions, lease fencing, atomic claim/complete, and restart recovery; it
still delegates provider execution and validation to the existing explicit
adapter contract. Use `--backend json --run-dir "<run-dir>"` only when
continuing a legacy JSON queue deliberately.

Resource-bearing planner packets also receive a schema-v3 fenced resource
reservation before a strict SQLite CLI claim. The reservation records the
RAM/CPU/GPU/disk request and governor admission; missing live evidence blocks
the new packet while legacy packets without a resource request remain on the
migration path. Audit old JSON runs with `lad legacy-import --root PATH` and
render the compact read-only L0 status with `lad cockpit --snapshot PATH`.

`lad plan` and the default `lad monitor` mode are read-only with respect to
providers: quota and SSH refreshes are opt-in. `lad run` is an explicit
execution command and preserves configured provider credentials; all packets
must still carry an exact model, placement, validation, write scope, and
artifact contract.

`lad replan` is an observation boundary: it separates provider failures from
compute/workload-host failures and emits reviewable constraints, but it never
enqueues a retry or starts a provider on its own.

For a verified SSH host, `scripts/remote_worker_client.py` is the only bundled
transport seam for the durable worker. It validates the private inventory,
keeps `execution_host` separate from `workload_host`, sends a redacted packet
over SSH stdin, and is dry-run by default; `--execute` is required to open an
SSH session. It is not a generic shell runner and does not download data or
invoke a provider. Unfenced artifact reads use `observe_artifacts`; manifest
mutation requires the current worker lease token. On reconnect after chat/SSH
loss, use the client's `resume`/`recover-handoff` operation: it atomically
reconciles an expired lease and returns a redacted handoff with the next claim
or review action. Known local absolute packet paths are mapped to the selected
host's declared project root; paths outside the captured workspace fail closed.

## OpenCode Go

OpenCode Go is a hosted subscription reached through a locally authenticated
CLI. It is not the free `opencode/*` Zen catalog and it is not a server-local
model runtime. Exact model IDs use `opencode-go/<model-id>`, while all Go
members consume one logical scheduler pool: `opencode.go`.

Run the read-only snapshot after system discovery:

```bash
python3 $HOME/.codex/skills/local-agent-dispatch/scripts/opencode_go_snapshot.py
```

The snapshot reads the installed version, provider configuration state, exact
Go catalog, and local historical stats without reading credential values or
sending a prompt. Treat local `opencode stats` as historical cost/token
evidence only. The catalog itself does not contain remaining allowance. An
explicit authenticated read-only call to the documented
`GET https://opencode.ai/zen/go/v1/usage` route is supported by
`opencode_go_quota_snapshot.py --usage-api --usage-use-auth-store`; it records
rolling/five-hour, weekly, and monthly account-level evidence with a five
minute TTL in the shared `opencode.go` pool. If the call fails, the balance
remains `unknown`; per-model balance and Zen overage amount also remain
unknown. The key is held in memory only and never emitted.

Choose exact models dynamically inside the shared pool:

- efficient/short work: prefer `opencode-go/mimo-v2.5`;
- bounded code: prefer `opencode-go/kimi-k2.7-code`;
- hard work: prefer `opencode-go/gpt-5.6-luna` with advertised `max`, then
  other fresh hard-role candidates.

These are role preferences, not a static allowlist. Refresh the exact catalog
each run, preserve the live per-model price metadata, and honor any advertised
usage multiplier. A model/variant capability rejection disables only that exact
tuple; explicit quota/auth/network evidence affects the shared `opencode.go`
pool. A misspelled or unadvertised variant fails closed.

DeepSeek remains excluded by default. If the user explicitly requests an exact
visible OpenCode Go DeepSeek member for a bounded parallel lane, the task may
carry `allow_policy_excluded_models: ["opencode-go/deepseek-v4-flash"]` plus an
exact `model_by_pool` override. The planner records
`model_role=explicit_policy_override`, keeps it in the single `opencode.go`
quota pool, and does not treat this as a quota workaround.

For durable execution, use the controller's `adapter="opencode"` or the guarded
runner:

```bash
python3 $HOME/.codex/skills/local-agent-dispatch/scripts/opencode_guarded_run.py \
  --cwd "<trusted-workspace>" \
  --model opencode-go/mimo-v2.5 \
  --prompt-file "<task-packet.md>" \
  --result-source "<fresh-final-output.txt>"
```

The runner sends the task over stdin, so prompt text is absent from argv and no
attachment capability is required. `--pure` is on by default. Never enable
OpenCode's dangerous `--auto` implicitly; it requires the explicit
`--auto-approve` adapter option and an independently bounded write scope.

### Server-side OpenCode Go execution

When the local machine is under RAM, swap, or disk pressure, keep the
controller and human approval local but run the OpenCode Go child agent on a
verified SSH host. Install the same OpenCode version on that host and
authenticate there with `opencode auth login`; do not copy the local
`~/.local/share/opencode/auth.json` or any credential value into the repository
or task packet. The bundled `scripts/remote_opencode_client.py` is dry-run by
default and, with explicit `--execute`, sends only the prompt file over SSH
stdin to `scripts/opencode_remote_run.py`. The remote wrapper enforces an exact
`opencode-go/<model-id>`, confined cwd/result path, timeout, JSON event/error
gate, and SHA-256 result summary. It does not install models, discover quota,
or accept an arbitrary shell command. Server-side Go usage remains part of the
single `opencode.go` pool; the planner must record the remote host and exact
model/variant in the attempt receipt.

## Cursor Quota Pools

Treat Cursor billing capacity as two shared pools, not one quota per model:

| Pool id | Members | Default role |
| --- | --- | --- |
| `cursor.composer_grok` | Composer and Grok models | broad throughput, drafts, bounded implementation, alternate reasoning |
| `cursor.other` | every other Cursor model | GPT/Codex/Gemini/Claude/Kimi/GLM work selected by task fit |

Important accounting rules:

- Composer and Grok consume the same logical pool. A limit or rate failure from
  either model reduces the health/capacity of `cursor.composer_grok` as a whole.
- All other models share `cursor.other`. Switching from one member to
  another does not escape exhaustion of that pool.
- `auto` is not dispatched automatically until its pool accounting is known.

Refresh the catalog at the start of an important run:

```bash
cursor-agent status --format json
cursor-agent about --format json
cursor-agent --list-models
```

The 2026-08-01 live Cursor Agent snapshot (`2026.07.23-e383d2b`, authenticated
Pro account) includes these families:

- Shared Composer/Grok pool:
  - `composer-2.5`, `composer-2.5-fast`
  - `cursor-grok-4.5-low|medium|high` and `*-fast` siblings
- Other pool:
  - `gpt-5.3-codex-low|medium|high|xhigh` and available `*-fast` siblings
  - `gpt-5.6-luna-none|low|medium|high|xhigh|max` and `*-fast` siblings
  - GPT-5.5 and GPT-5.4 families, including Mini/Nano variants
  - `gemini-3.6-flash-minimal|low|medium|high`, `gemini-3.1-pro`,
    `gemini-3.5-flash`, and `gemini-3-flash`
  - `kimi-k3-low|high|max`, `kimi-k2.7-code`
  - `glm-5.2-high|max`
  - `claude-sonnet-5-*`, `claude-opus-5-*`, `claude-opus-4-8-*`,
    `claude-fable-5-*`, and older Sonnet/Opus families returned by the live list

The live `--list-models` result is authoritative only for the exact spelling
and catalog visibility of a model at that moment. It is not proof that the
current account, workspace, request endpoint, or remaining quota can execute
that model. Record a returned ID as `catalog_visible`, not `ready`. Never invent
a suffix from a family pattern; choose an exact returned ID.

Track Cursor catalog and runtime eligibility separately:

```text
model_id
catalog_state       # visible, absent, unknown
runtime_state       # accepted, rejected, unknown
runtime_reason      # exact latest request-end error or none
last_runtime_success
last_checked_at
```

A successful real request is the strongest readiness evidence. A real request
rejection overrides catalog visibility for scheduling until a later successful
request or newer explicit capability evidence. Do not spend quota on a probe
only to convert `runtime_state=unknown`; let the first authorized real job be
the probe and preserve its exact failure text.

Persist this model state in the run's `execution_plan.md`/`state.json` and carry
the newest same-day evidence into a continuation. A catalog refresh alone must
not erase a recorded runtime rejection or quota cooldown.

Cursor direct command shape:

```bash
cursor-agent -p "<task>" --trust --force \
  --workspace "<trusted-workspace>" --model "composer-2.5-fast"
```

Cursor exposes authentication, subscription tier, and model catalog in the
CLI, but not a reliable remaining-quota number. Use runtime rate-limit/errors
as pool backpressure. Read the desktop Plan & Usage surface only when the user
explicitly authorizes a focus-stealing UI check.

Do not conflate model eligibility with shared-pool capacity. In particular,
`Cannot use this model: <id>. Available models:` is an execution-gate rejection,
not by itself a quota failure. Mark that exact model `runtime_state=rejected`.
If the returned alternatives are blank, preserve that fact, but do not replace
a successful catalog snapshot with an empty global catalog.

## Antigravity Quota and Health

Antigravity has two different interactive surfaces:

- `/usage`: model quota. This is the primary scheduler signal. It shows model
  groups plus five-hour and weekly limits.
- `/credits`: the separate G1 AI-credit wallet. A value of zero here does not
  prove the `/usage` model quota is exhausted.

Do not run `antigravity usage` or `antigravity credits` as shell subcommands.
Start the interactive CLI and type `/usage`.

For a repeatable model-quota snapshot, use:

```bash
python3 $HOME/.codex/skills/local-agent-dispatch/scripts/antigravity_usage_tui_snapshot.py \
  --cwd "<trusted-workspace>"
```

Why older reads failed frequently:

1. they opened `/credits`, which is not the model-quota panel;
2. Antigravity briefly renders "not signed in" before restoring OAuth from the
   keyring;
3. a fixed-delay helper could send the slash command before the TUI was ready;
4. a non-capable `TERM` or a short terminal height could hide the panel.

The usage helper must wait for the authenticated TUI prompt, force a capable
terminal, use a tall PTY, retry the slash command once, and preserve diagnostic
fields when parsing still fails.

### Antigravity shared pools

Map the `/usage` groups exactly as displayed:

| Pool id | Shared members | Best role |
| --- | --- | --- |
| `antigravity.gemini` | Gemini Flash and Gemini Pro | Flash for efficient throughput; Pro for hard reasoning and independent critique |
| `antigravity.claude_gpt` | Claude Opus, Claude Sonnet, and GPT-OSS | Sonnet for normal strong work, Opus for premium/final work, GPT-OSS for diversity or an alternate implementation |

All members inside a row share both the weekly limit and the five-hour limit.
Changing models inside the same row does not create more quota. A quota or rate
failure from one member reduces the health of that entire pool.

For each pool, compute:

```text
effective_available_percent = min(weekly_percent, five_hour_percent)
```

Both windows are hard gates. Use the lower displayed percentage for scheduling.
The snapshot supplied by the user on 2026-08-01 shows `100.00%` and `Quota
available` for both windows in both pools, so both Antigravity pools are
currently `ready`. This does not override runtime auth, network, or stall checks.

Use these default dynamic bands; rescore at job boundaries:

| Effective available | Pool policy |
| --- | --- |
| `70-100%` | healthy: use normally; prefer efficient models for bulk work and stronger members only when task difficulty justifies them |
| `40-70%` | balanced: reduce concurrency and prefer Flash/Sonnet or shorter prompts |
| `15-40%` | conserve: stop bulk shards; reserve for hard, high-value, or independent-review jobs |
| `<15%` | drain/preserve: finish useful in-flight work, then pause new work unless explicitly required |
| `0%` or `Quota unavailable` | blocked until the affected window becomes available again |

Prefer the precise percentage beside the progress bar over the rounded
`NN% remaining` sentence. Parse `Refreshes in ...` when present. If the
five-hour refresh is within about 30 minutes or the weekly refresh is within
about 90 minutes, reduce the reserve penalty for high-value jobs that can make
durable progress before the refresh; if the pool is nearly empty, queue long
jobs for the refresh instead of launching them into an imminent limit.

Quota is consumed proportionally to token cost. Prefer bounded prompts, scoped
inputs, explicit output files, and concise review packets. Do not spend Pro or
Opus on mechanical extraction that Flash, Spark, or Composer can finish.

Before an Antigravity run:

```bash
antigravity models
python3 $HOME/.codex/skills/local-agent-dispatch/scripts/antigravity_usage_tui_snapshot.py \
  --cwd "<trusted-workspace>"
```

Use only exact canonical slugs returned by the current `antigravity models`,
including Gemini, Claude, and GPT-OSS entries. Do not reconstruct a slug from
the display name.

For long/background work, use the guarded runner:

The guarded runner is an optional local adapter and is not bundled with the
open-source core. Configure its audited path through `ANTIGRAVITY_GUARDED_RUN`;
the controller fails closed when the path is absent.

```bash
python3 $HOME/.codex/skills/claude-usage-scheduler/scripts/antigravity_guarded_run.py \
  --cwd "<trusted-workspace>" \
  --prompt-file "<prompt.md>" \
  --model gemini-3.6-flash-high \
  --print-timeout 45m \
  --idle-timeout 150 \
  --watch-path "<worker-output-dir>" \
  --require-path "<worker-output-dir>/status.md" \
  --log "<agent-log>" \
  --stall-marker "<agent-log>.stall.md"
```

Use the same guarded shape with `claude-sonnet-4-6`,
`claude-opus-4-6-thinking`, or `gpt-oss-120b-medium` when the
`antigravity.claude_gpt` pool is selected and the exact slug is present in the
same tick's live catalog.

Catalog success alone is not proof that a real prompt can finish. Cool down
the affected shared Antigravity pool after quota/model failures and both pools
after provider-wide auth, TLS, idle-timeout, or zero-progress failures. Do not
rotate names inside one exhausted shared pool as a quota workaround.

## Dynamic Scheduler

### 1. Describe every job

```text
job_id
task_type          # code, text, research, audit, monitor
difficulty         # L0-L5
latency_priority   # low, normal, high
can_split
depends_on
write_scope
required_artifact
needs_independent_review
```

### 2. Track state per quota pool

```text
pool_id
health             # ready, degraded, cooldown, blocked, unknown
quota_display      # exact UI/CLI display or unknown
weekly_percent
five_hour_percent
effective_percent  # minimum of the two when both exist
reset_horizon
inflight
recent_failures
last_success
last_checked_at
```

Never create separate capacity counters for models that share a Cursor or
Antigravity pool, or for models inside OpenCode Go. Per-model runtime
eligibility is not a capacity counter; keep it alongside the shared-pool state
so an unsupported model or variant does not incorrectly disable its siblings.

Unknown quota is a hard planning block by default. A provider preflight may
explicitly set `unknown_quota_policy=pilot` and a small
`unknown_quota_pilot_percent` when it has authenticated/catalog evidence but no
numeric remaining-balance endpoint (currently Cursor, Antigravity, and
OpenCode Go use this bounded opt-in). The planner records the policy and never
turns unknown into 0%, 100%, or a fabricated full budget.

### 3. Select with a changing score

```text
score =
  task_fit
  + speed_fit
  + quality_per_estimated_quota_unit
  + user_primary_model_bonus
  + available_quota_bonus
  + diversity_bonus
  - shared_pool_pressure
  - recent_failure_penalty
  - expensive_model_penalty
  - stall_penalty
```

Recompute after every job completion, failure, quota refresh, or new request.
Do not freeze the whole run to an initial static model list.

Estimate subscription usage from task duration times the latest observed
quota-percent rate. Separately, when the task has input/output token bounds and
the provider exposes per-million-token prices, record `estimated_usd_cost` in
the assignment. Missing price or token evidence is `unknown`, never zero. Use
Luna/max's low-cost prior when no better usage measurement exists; do not avoid
Luna merely because its remaining percentage is lower. Calibrate model/pool
rates from before/after usage snapshots and preserve attribution uncertainty.

Use rolling-horizon dynamic programming plus the three-minute feedback loop in
`references/dynamic_planning.md`. Plan only the next wave, monitor logs,
processes, artifacts, failures, and quota deltas, then solve again from the
updated state.

For data, environments, CPU/GPU workloads, and remote execution, read
`references/compute_routing.md`. Probe hosts with `compute_resource_probe.py`,
apply the server/data gates, then jointly reserve model-pool and host resources.
Use `$HOME/.codex/local-agent-dispatch/hosts.json` as the current private
host inventory; refresh it from the user's latest endpoints before probing.

Treat model-process placement and workload-compute placement as separate
fields. Codex, OpenCode Go, Cursor, and Antigravity use desktop-authenticated
CLIs and their `execution_host` must remain a reachable local host. A server-first workload
may independently use `workload_host` on SSH. A server-local model binds both
fields to its owning server. Do not interpret a remote workload placement as
permission to run a desktop-authenticated CLI remotely.

For Cursor, a route is eligible only when its pool is schedulable, its exact ID
is present in a fresh catalog snapshot, and its latest runtime state is not
`rejected`. `runtime_state=unknown` may be used for the first authorized real
job, but never be reported as confirmed ready.

For chat-quota continuity, read `references/quota_continuity.md`. Run
`dispatch_preflight_scan.py` before planning, persist task packets and fallback
chains, and start `continuity_controller.py` before Codex becomes unavailable.
The controller may continue queued work through local OpenCode Go,
Cursor/Antigravity, or a loopback server-local model API; it cannot infer new user intent after the chat
stops. When Codex returns, import its resume state and validate artifacts before
replanning unfinished jobs.

The controller persists real invocation outcomes in
`$HOME/.codex/local-agent-dispatch/runtime-state.json`. Explicit quota,
authentication, network, or capability failures override catalog-derived
health in later preflights until the recorded cooldown expires or a newer real
success clears it. A catalog refresh alone must never clear this state.

For a noisy command adapter such as `codex exec --json`, set both
`result_source_path` and `output_path`, and make the CLI write only its final
answer to the result source (for Codex, use `-o`/`--output-last-message`). The
controller keeps combined stdout/stderr in its attempt log and publishes only a
fresh, nonempty result source as the required artifact. Missing or stale result
sources fail closed. Antigravity routes child stdout to the result source when
that field is present.

### 4. Default routing

| Need | First choice | Alternate |
| --- | --- | --- |
| monitor/status only | local deterministic checks | none |
| tiny bounded code loop | Codex Spark | OpenCode Go MiMo or Cursor Composer fast |
| ordinary implementation | Codex Luna/max | OpenCode Go Kimi Code or Cursor Codex 5.3/Composer |
| bulk independent drafting | Cursor Composer/Grok shared pool | Cursor other pool |
| hard reasoning/audit | Codex Luna/max | OpenCode Go Luna/max or strong model in Cursor other pool |
| independent provider critique | Antigravity Gemini Pro or GPT-OSS when its `/usage` pool is healthy | Cursor model from a different family |
| premium alternate audit | Antigravity Opus when `antigravity.claude_gpt` is healthy | Codex Luna/max |

The two Codex CLI models remain primary. OpenCode Go adds a low-cost throughput
and continuity lane; Cursor and Antigravity provide further pool balancing or
independent critique. None silently overrides an explicit user model request.

### 5. Pool-level failure behavior

- Composer reports an explicit usage/rate limit -> cool down
  `cursor.composer_grok`, including Grok.
- Grok reports an explicit usage/rate limit -> cool down
  `cursor.composer_grok`, including Composer.
- Any other Cursor model reports an explicit shared usage/rate failure -> cool
  down `cursor.other`; changing to another member is not a quota workaround.
- Cursor reports `Cannot use this model`, `unsupported model`, `not entitled`,
  or an equivalent capability rejection without an explicit usage/rate signal
  -> reject only that exact model tuple. Do not infer that its shared quota pool
  or sibling models are exhausted.
- Cursor catalog visibility followed by request rejection -> trust the request
  endpoint for runtime eligibility and retain the catalog result only as
  candidate-discovery evidence.
- Antigravity `/usage` unavailable but the TUI is still authenticating -> retry
  readiness once; then mark quota unknown, not zero.
- Gemini Flash/Pro quota failure -> cool down `antigravity.gemini` as a whole.
- Antigravity Opus/Sonnet/GPT-OSS quota failure -> cool down
  `antigravity.claude_gpt` as a whole.
- Provider-wide Antigravity auth, TLS, or zero-progress failure -> cool down
  both Antigravity pools.
- Antigravity `/credits` equals zero while `/usage` says model quota available
  -> keep the G1 wallet exhausted flag separate from model-pool health.
- Codex model/effort preflight fails -> do not dispatch; refresh the cache and
  choose only an approved exact pair.
- OpenCode Go reports a model/variant capability error -> reject only that
  exact tuple and try the next eligible role candidate; keep sibling models.
- OpenCode Go reports quota or authentication failure -> cool or block the
  shared `opencode.go` pool; switching model IDs is not a quota workaround.

### 6. Parallel and serial topology

Use parallel shards only when each worker has an independent input and output
path. Use serial execution when one result changes the next task, when workers
would edit the same file, or when a single coherent decision is required.

For a parallel run, assign at most one writer per write scope and record:

```text
worker_id -> pool_id -> exact_model -> output_path -> status -> last_progress
```

Use the template at:

```text
$HOME/.codex/skills/local-agent-dispatch/templates/parallel_execution_plan.md
```

### 7. Provider-free task capture and history calibration

Before planning a natural-language request, the bundled capture boundary can
create a reviewable `TaskPacket` without contacting any provider:

```bash
python3 $HOME/.codex/skills/local-agent-dispatch/scripts/task_capture.py \
  --task task.json --repo-root "<trusted-workspace>" \
  --history observations.json --model gpt-5.3-codex-spark \
  --host remote-a
```

It records bounded repository metadata, an explicit or conservative inferred
DAG, deterministic topological order/parallel waves, per-node P50/P90
estimates, and exact task-family/model/host historical EWMA calibration.  It
reads no file contents, executes no project command, and sends no prompt.
Unknown resources and missing history remain `unknown`; an unknown dependency
or cycle produces `dag_invalid` and must be reviewed before dispatch.  The
packaged equivalent is `lad capture --task ...`.

## Safe Dispatch Procedure

1. Confirm the real workspace and requested write scope, then run
   `local_system_scan.py`; stop local bulk work when its capacity gate closes.
2. Classify dependencies and estimate input/download/environment/temp/cache/
   output disk, RAM, CPU, GPU/VRAM, agent time, and workload time.
3. Refresh relevant models/quotas and lightly probe candidate compute hosts.
   Treat detected external agent processes as pool inflight and keep quota-rate
   attribution non-exclusive unless run ownership is explicitly established.
4. Apply hard resource, path, data-route, privacy, server-first, and billing
   gates; update pool/model/host state.
5. Run the rolling-horizon planner and dispatch only its first wave.
6. Preview the command and possible quota spend when required.
7. Dispatch explicitly with logs and a required artifact for background work.
8. Monitor for 180 seconds by default at 30-second intervals; watch process,
   log/artifact growth, quota, reachability, CPU/load, RAM, disk, GPU/VRAM, and
   billing-relevant state.
9. On failure, classify the exact error first: reject one model for capability
   failures, or cool down the affected shared pool for explicit quota/rate
   failures.
10. Feed observations and measured price back into state, replan, then keep,
    reroute, drain, or pause.
11. Validate the resulting artifact before marking the job complete.

Do not claim success from a launched PID alone. Do not run a paid smoke prompt
unless the user authorized provider spend. Preserve user files and avoid
multiple agents editing the same target.

## Status Format

```text
active: <workers and exact models>
pools: <health/quota summary for codex, cursor.composer_grok, cursor.other,
        antigravity.gemini, antigravity.claude_gpt, opencode.go>
progress: <done/pending/failed and latest artifact>
decision: keep / reroute / drain / pause / escalate
next: <next scheduler action>
```

## Verification After Editing This Skill

```bash
python3 $HOME/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  $HOME/.codex/skills/local-agent-dispatch
python3 -m py_compile \
  $HOME/.codex/skills/local-agent-dispatch/scripts/codex_model_preflight.py \
  $HOME/.codex/skills/local-agent-dispatch/scripts/antigravity_usage_tui_snapshot.py \
  $HOME/.codex/skills/local-agent-dispatch/scripts/codex_usage_snapshot.py \
  $HOME/.codex/skills/local-agent-dispatch/scripts/local_system_scan.py \
  $HOME/.codex/skills/local-agent-dispatch/scripts/opencode_go_snapshot.py \
  $HOME/.codex/skills/local-agent-dispatch/scripts/opencode_guarded_run.py \
  $HOME/.codex/skills/local-agent-dispatch/scripts/compute_resource_probe.py \
  $HOME/.codex/skills/local-agent-dispatch/scripts/dynamic_dispatch_planner.py \
  $HOME/.codex/skills/local-agent-dispatch/scripts/dispatch_monitor.py \
  $HOME/.codex/skills/local-agent-dispatch/scripts/server_local_model_scan.py \
  $HOME/.codex/skills/local-agent-dispatch/scripts/dispatch_preflight_scan.py \
  $HOME/.codex/skills/local-agent-dispatch/scripts/task_capture.py \
  $HOME/.codex/skills/local-agent-dispatch/scripts/dispatch_workflow.py \
  $HOME/.codex/skills/local-agent-dispatch/scripts/remote_worker.py \
  $HOME/.codex/skills/local-agent-dispatch/scripts/remote_worker_client.py \
  $HOME/.codex/skills/local-agent-dispatch/scripts/continuity_controller.py
bash -n $HOME/.codex/skills/local-agent-dispatch/scripts/*.sh
```
