# Plugin protocol boundary

Phase 2 introduces a small, stdlib-only package seam at
`src/local_agent_dispatch/plugins/`. It is deliberately separate from the
standalone files under `scripts/`, so existing `lad` flows and offline
fixtures do not need to import a provider SDK.

## Five plugin kinds

Each plugin exposes a static `PluginDescriptor` named `descriptor`:

| Kind | Required operations | Role |
| --- | --- | --- |
| `system_probe` | `probe(request)` | Read local OS, hardware, and installed-runtime evidence |
| `provider` | `discover_catalog`, `discover_auth_state`, `discover_quota`, `probe_runtime`, `execute` | Separate provider catalog/auth/quota/runtime evidence from execution |
| `runtime` | `probe`, `execute` | Use a local or server model runtime such as vLLM, Ollama, or llama.cpp |
| `transport` | `prepare`, `execute` | Move a bounded workspace/artifact through local or SSH transport |
| `validator` | `validate(request)` | Independently check output/artifact freshness and quality |

The request and result dataclasses are intentionally small and contain paths
or references, not an implicit network client. `Evidence(status="unknown")`
is a first-class result: missing quota or an unreachable runtime must not be
converted into `ready` or `zero` by a plugin.

## Registration and conformance

`PluginRegistry` uses an explicit `register(plugin)` call. It does not scan
Python entry points, import optional packages, or run a probe while registering
one. `conformance_report(plugin)` only checks the descriptor and callable
method surface. This keeps `lad doctor --offline`, CI, and preflight free of
provider prompts and SSH connections.

`register_many()` returns one report per plugin and isolates a malformed plugin.
After a lease/policy gate is held, a controller may explicitly call
`registry.invoke(kind, plugin_id, operation, request)`. A plugin exception is
returned as a redacted local failure result and does not tear down sibling
lanes.

The registry is not a trust boundary. A future provider implementation still
needs the existing path confinement, secret filtering, quota, host, and
validator gates before it can be enabled. The current Phase 2 scope is the
protocol and conformance seam; migration of the existing provider scripts and
real CLI/SSH calls remains a later acceptance gate.

## Minimal implementation

```python
from local_agent_dispatch.plugins import Evidence, PluginDescriptor, ProbeRequest

class DarwinProbe:
    descriptor = PluginDescriptor("darwin", "system_probe", capabilities=("cpu", "ram"))

    def probe(self, request: ProbeRequest) -> Evidence:
        return Evidence("unknown", reason="probe implementation not enabled")
```

This example is provider-free. A real adapter should return fresh evidence
with an explicit source and status, and should never claim that an unavailable
catalog, auth state, runtime, or quota is ready.
