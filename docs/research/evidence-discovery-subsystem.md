# Evidence Discovery and Compatibility Resolver

## Why this is a separate subsystem

Provider catalogs, quota dashboards, CLI flags, API routes, runtime behavior,
and account policies change independently. A model being visible in a catalog
does not prove that a request is accepted, that a quota balance is readable, or
that a fallback wallet is disabled. Hard-coding one provider's current route in
the planner turns a temporary observation into a false capability.

The resolver therefore runs before planning and before any paid or irreversible
execution:

```text
Question/need
  -> source discovery
  -> source ranking and version match
  -> redacted capability extraction
  -> bounded probe (only if authorized)
  -> evidence record + TTL + confidence
  -> planner gate / explicit unknown
```

## Source hierarchy

1. Provider-maintained documentation and API/OpenAPI source;
2. Provider repository release, tag, route implementation, and changelog;
3. Provider issue/PR describing current behavior, with merge/release status;
4. Local CLI help/version and read-only command output;
5. Local runtime response, structured error, or usage receipt;
6. User-supplied console export or manual observation;
7. Unverified community discussion (discovery hint only, never a readiness
   gate).

Every record stores URL or local command provenance, retrieval time, observed
software version, source kind, evidence hash, freshness TTL, and a confidence
grade. Search results are not themselves capabilities.

## Capability record

The first implementation should be provider-neutral and cover:

- `catalog`: exact model ID, variants, context, tools, attachments, pricing;
- `auth`: configured/not configured, credential source kind (never value);
- `quota`: window, used/remaining, reset, scope, attribution, fallback wallet;
- `runtime`: accepted/rejected model+variant, error class, retry/reset hints;
- `transport`: control, artifact, bulk-data, execution and workload routes;
- `resource`: host, mount, writable path, VRAM/RAM/disk/inode/quota evidence;
- `policy`: user opt-in, excluded model, spending/parallelism cap;
- `probe`: command, timeout, network/side-effect class, result and redaction.

The resolver must distinguish `visible`, `configured`, `reachable`, `accepted`,
`quota_known`, and `ready`; no one status may imply another. A failed or stale
probe only invalidates the narrow capability it measured unless the error is a
shared-pool, authentication, or transport failure.

## OpenCode Go example

The OpenCode Go catalog and `opencode stats` are not current balance evidence.
The official service now exposes a read-only Go usage route:
`GET https://opencode.ai/zen/go/v1/usage` with a bearer key. The resolver should
use it only through an explicit, redacted credential boundary, record the exact
software/API evidence, and normalize its used percentages into remaining
percentages without losing the original values. `rolling`, `weekly`, and
`monthly` remain one `opencode.go` shared pool. `useBalance`/Zen fallback is a
separate policy field, not free quota. If the endpoint is unavailable, the
resolver falls back to console import, receipts, history, and runtime errors;
it never converts unavailable data to zero or full.

Search is a compatibility operation, not a source of truth by itself. It may
discover an official document, merged upstream change, documented endpoint, or
version-specific CLI behavior. Each discovery should carry its URL, observed
version, retrieval time, evidence hash, TTL, and confidence. Search can propose
a probe; only a successful local/remote probe or an explicit user snapshot can
change planner readiness. This is essential when a catalog advertises a model
but the execution endpoint rejects it.

Resource discovery follows the same rule. A host is not represented by one
root-directory `df` value or one RAM number: the digital twin must preserve
mounts, writable project paths, available RAM, swap state, memory-pressure
signals, load, and observed agent lanes. Exhausted swap plus low headroom is a
local admission stop even when the nominal RAM percentage looks acceptable;
compatible work should be placed on a verified remote mount instead.

## Safe search/probe contract

- Search is read-only and may use official web/API/repository sources.
- A local probe has an allowlisted executable, fixed timeout, bounded output,
  scrubbed environment, and an explicit side-effect class.
- A paid model prompt, SSH command, download, or credential use requires an
  execution policy approval separate from discovery.
- Endpoint probing is opt-in when it sends an authenticated request, even if
  the request is read-only; the request and response metadata are logged, but
  no token or raw sensitive response is persisted.
- Contradictory sources produce `discrepancy=true` and lower confidence; they do
  not get averaged into a fabricated number.
- Unknown quota/resource evidence yields a bounded pilot or a fail-closed gate,
  chosen by policy and recorded in the plan.

## Research and evaluation

The resolver is evaluated on a frozen provider/version corpus and replayed with
time-shifted source records. Primary metrics are: correct endpoint discovery,
false-ready rate, stale-source rejection, probe cost, discrepancy detection,
and time-to-compatible-plan. A release gate requires that an unsupported or
stale model is never marked ready solely from catalog visibility, and that a
newly discovered official endpoint can be added as a driver without changing
the planner's core.

## Current implementation

The provider-neutral pure functions live in
`src/local_agent_dispatch/discovery/resolver.py`:

- `build_search_plan` creates official-docs/source/release/issue queries;
- `build_probe_plan` describes a bounded, prompt-free probe without executing
  it;
- `resolve_capability` ranks version-matched evidence and fails closed on
  stale or contradictory claims;
- `resolve_gate` requires explicit statuses such as `visible`, `accepted`, and
  `quota_known` instead of inferring readiness.

The implementation is deliberately offline. A future web-search adapter and
provider adapter may feed it redacted records, but they must preserve the
source URL, version, timestamp, TTL, and side-effect class.

For the OpenCode Go case, the official repository records the endpoint change
in [PR #16513](https://github.com/anomalyco/opencode/pull/16513), while the
[Go plan documentation](https://opencode.ai/docs/go) remains the source for
plan windows and pricing. The older
[feature issue #16017](https://github.com/anomalyco/opencode/issues/16017)
is retained as historical evidence that the API was previously absent; it must
not override the merged implementation.
