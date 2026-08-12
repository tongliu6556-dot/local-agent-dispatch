"""Stable, provider-free plugin contracts for local-agent-dispatch.

The dispatch scripts intentionally remain usable as standalone files.  This
module is the small package-level seam that new integrations can implement
without importing a provider SDK or opening a network connection.  Protocols
describe the operations; the registry/conformance module checks metadata and
method presence before a plugin is admitted to a control-plane process.

The operation methods are deliberately split by evidence type.  A provider
whose quota probe fails must return an ``Evidence`` value with ``status`` set
to ``unknown``/``error``; it cannot manufacture a ready catalog or quota from
that failure.  None of the dataclasses below performs discovery or execution.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable


PLUGIN_API_VERSION = "1"

PLUGIN_KINDS = (
    "system_probe",
    "provider",
    "runtime",
    "transport",
    "validator",
)

EVIDENCE_STATUSES = (
    "ready",
    "unknown",
    "blocked",
    "unavailable",
    "error",
)

EvidenceStatus = Literal[
    "ready", "unknown", "blocked", "unavailable", "error"
]


def _copy_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Copy a mapping so frozen result objects do not alias caller state."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("plugin metadata must be a mapping")
    return dict(value)


@dataclass(frozen=True)
class PluginDescriptor:
    """Static identity and capability metadata for one plugin.

    ``plugin_id`` is scoped by ``kind`` in the registry, so ``local`` may be
    used once as a transport and once as a runtime.  Capabilities are labels,
    not permission grants; the planner still applies host, quota, and policy
    gates before any operation is invoked.
    """

    plugin_id: str
    kind: str
    version: str = "0.1.0"
    api_version: str = PLUGIN_API_VERSION
    capabilities: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _copy_mapping(self.metadata))
        object.__setattr__(self, "capabilities", tuple(self.capabilities))


@dataclass(frozen=True)
class ProbeRequest:
    """Bounded context for local system discovery."""

    workspace: str | None = None
    host_id: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", _copy_mapping(self.options))


@dataclass(frozen=True)
class DiscoveryRequest:
    """Provider/runtime discovery context with no prompt payload."""

    scope: str = "default"
    host_id: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", _copy_mapping(self.options))


@dataclass(frozen=True)
class ExecutionRequest:
    """Opaque execution context supplied only after policy/lease gates."""

    job_id: str
    attempt_id: str
    workspace: str
    model: str | None = None
    variant: str | None = None
    prompt_file: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", _copy_mapping(self.options))


@dataclass(frozen=True)
class TransportRequest:
    """Transport preparation/execution context.

    The request carries references rather than prompt text.  Concrete
    transports are responsible for their own confinement and authentication
    checks; this package never opens SSH or starts a subprocess.
    """

    job_id: str
    attempt_id: str
    source: str | None = None
    destination: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", _copy_mapping(self.options))


@dataclass(frozen=True)
class ValidationRequest:
    """Artifact validation context supplied after an execution attempt."""

    job_id: str
    attempt_id: str
    workspace: str
    artifacts: tuple[str, ...] = ()
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(self, "options", _copy_mapping(self.options))


@dataclass(frozen=True)
class Evidence:
    """Evidence returned by discovery/probe operations.

    ``status=unknown`` is intentional and distinct from ``ready``.  Consumers
    must not reinterpret absent or failed evidence as an available resource.
    """

    status: EvidenceStatus = "unknown"
    data: Mapping[str, Any] = field(default_factory=dict)
    source: str | None = None
    observed_at: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in EVIDENCE_STATUSES:
            raise ValueError(f"unsupported evidence status: {self.status}")
        object.__setattr__(self, "data", _copy_mapping(self.data))


@dataclass(frozen=True)
class ExecutionResult:
    """Provider/runtime execution result; completion still needs validation."""

    status: EvidenceStatus = "unknown"
    output: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    artifacts: Mapping[str, Any] = field(default_factory=dict)
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in EVIDENCE_STATUSES:
            raise ValueError(f"unsupported execution status: {self.status}")
        object.__setattr__(self, "data", _copy_mapping(self.data))
        object.__setattr__(self, "artifacts", _copy_mapping(self.artifacts))


@dataclass(frozen=True)
class ValidationResult:
    """Validator result; only ``ready`` with explicit artifact data can pass."""

    status: EvidenceStatus = "unknown"
    passed: bool = False
    data: Mapping[str, Any] = field(default_factory=dict)
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in EVIDENCE_STATUSES:
            raise ValueError(f"unsupported validation status: {self.status}")
        object.__setattr__(self, "data", _copy_mapping(self.data))


@runtime_checkable
class Plugin(Protocol):
    """Common static surface shared by all plugin kinds."""

    @property
    def descriptor(self) -> PluginDescriptor:
        ...


@runtime_checkable
class SystemProbe(Plugin, Protocol):
    """Discover local OS/hardware/runtime facts without provider prompts."""

    def probe(self, request: ProbeRequest) -> Evidence:
        ...


@runtime_checkable
class ProviderAdapter(Plugin, Protocol):
    """Provider CLI/API boundary with separate evidence operations."""

    def discover_catalog(self, request: DiscoveryRequest) -> Evidence:
        ...

    def discover_auth_state(self, request: DiscoveryRequest) -> Evidence:
        ...

    def discover_quota(self, request: DiscoveryRequest) -> Evidence:
        ...

    def probe_runtime(self, request: DiscoveryRequest) -> Evidence:
        ...

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        ...


@runtime_checkable
class RuntimeAdapter(Plugin, Protocol):
    """Local/server model-runtime boundary (vLLM, Ollama, llama.cpp, etc.)."""

    def probe(self, request: DiscoveryRequest) -> Evidence:
        ...

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        ...


@runtime_checkable
class TransportAdapter(Plugin, Protocol):
    """Artifact/workspace transport boundary (local, OpenSSH, or future)."""

    def prepare(self, request: TransportRequest) -> Evidence:
        ...

    def execute(self, request: TransportRequest) -> Evidence:
        ...


@runtime_checkable
class Validator(Plugin, Protocol):
    """Independent artifact/quality validation boundary."""

    def validate(self, request: ValidationRequest) -> ValidationResult:
        ...


PROTOCOL_METHODS: Mapping[str, tuple[str, ...]] = {
    "system_probe": ("probe",),
    "provider": (
        "discover_catalog",
        "discover_auth_state",
        "discover_quota",
        "probe_runtime",
        "execute",
    ),
    "runtime": ("probe", "execute"),
    "transport": ("prepare", "execute"),
    "validator": ("validate",),
}


__all__ = [
    "PLUGIN_API_VERSION",
    "PLUGIN_KINDS",
    "EVIDENCE_STATUSES",
    "EvidenceStatus",
    "PluginDescriptor",
    "ProbeRequest",
    "DiscoveryRequest",
    "ExecutionRequest",
    "TransportRequest",
    "ValidationRequest",
    "Evidence",
    "ExecutionResult",
    "ValidationResult",
    "Plugin",
    "SystemProbe",
    "ProviderAdapter",
    "RuntimeAdapter",
    "TransportAdapter",
    "Validator",
    "PROTOCOL_METHODS",
]
