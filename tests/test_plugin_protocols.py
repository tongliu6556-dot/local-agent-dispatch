"""Provider-free conformance tests for the package plugin boundary."""

from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from local_agent_dispatch.plugins import (  # noqa: E402
    DiscoveryRequest,
    Evidence,
    ExecutionRequest,
    ExecutionResult,
    PluginDescriptor,
    PluginRegistry,
    PluginRegistryError,
    ProbeRequest,
    ProviderAdapter,
    RuntimeAdapter,
    SystemProbe,
    TransportAdapter,
    TransportRequest,
    ValidationRequest,
    ValidationResult,
    Validator,
    conformance_report,
)


class FakeSystemProbe:
    descriptor = PluginDescriptor(
        "fake-system",
        "system_probe",
        capabilities=("os", "cpu"),
    )

    def __init__(self) -> None:
        self.calls = 0

    def probe(self, request: ProbeRequest) -> Evidence:
        self.calls += 1
        return Evidence("ready", {"os": "fake"}, source="fixture")


class FakeProvider:
    descriptor = PluginDescriptor("fake-provider", "provider", capabilities=("catalog", "quota"))

    def discover_catalog(self, request: DiscoveryRequest) -> Evidence:
        return Evidence("ready", {"models": ["fake-model"]}, source="fixture")

    def discover_auth_state(self, request: DiscoveryRequest) -> Evidence:
        return Evidence("ready", {"configured": True}, source="fixture")

    def discover_quota(self, request: DiscoveryRequest) -> Evidence:
        return Evidence("unknown", reason="fixture does not model quota")

    def probe_runtime(self, request: DiscoveryRequest) -> Evidence:
        return Evidence("ready", {"runtime": "fake"}, source="fixture")

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        return ExecutionResult("ready", output="fake result")


class FakeRuntime:
    descriptor = PluginDescriptor("fake-runtime", "runtime", capabilities=("openai_compatible",))

    def probe(self, request: DiscoveryRequest) -> Evidence:
        return Evidence("ready", {"endpoint": "fixture"})

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        return ExecutionResult("ready", output="fixture")


class FakeTransport:
    descriptor = PluginDescriptor("fake-transport", "transport")

    def prepare(self, request: TransportRequest) -> Evidence:
        return Evidence("ready", {"prepared": True})

    def execute(self, request: TransportRequest) -> Evidence:
        return Evidence("ready", {"transferred": True})


class FakeValidator:
    descriptor = PluginDescriptor("fake-validator", "validator")

    def validate(self, request: ValidationRequest) -> ValidationResult:
        return ValidationResult("ready", passed=True, data={"fresh": True})


class CrashingProvider:
    descriptor = PluginDescriptor("crashing-provider", "provider")

    def discover_catalog(self, request: DiscoveryRequest) -> Evidence:
        raise RuntimeError("fixture provider failed")

    def discover_auth_state(self, request: DiscoveryRequest) -> Evidence:
        return Evidence("unknown")

    def discover_quota(self, request: DiscoveryRequest) -> Evidence:
        return Evidence("unknown")

    def probe_runtime(self, request: DiscoveryRequest) -> Evidence:
        return Evidence("unknown")

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        return ExecutionResult("error", reason="fixture")


class IncompletePlugin:
    descriptor = PluginDescriptor("incomplete", "provider")

    def discover_catalog(self, request: DiscoveryRequest) -> Evidence:
        return Evidence("unknown")


class PluginProtocolTests(unittest.TestCase):
    def test_all_five_protocols_have_a_provider_free_fake(self) -> None:
        self.assertIsInstance(FakeSystemProbe(), SystemProbe)
        self.assertIsInstance(FakeProvider(), ProviderAdapter)
        self.assertIsInstance(FakeRuntime(), RuntimeAdapter)
        self.assertIsInstance(FakeTransport(), TransportAdapter)
        self.assertIsInstance(FakeValidator(), Validator)

    def test_conformance_requires_only_static_metadata_and_methods(self) -> None:
        plugin = FakeSystemProbe()
        report = conformance_report(plugin)
        self.assertTrue(report.ok)
        self.assertEqual(("probe",), report.methods)
        self.assertEqual(0, plugin.calls)

    def test_registration_does_not_invoke_provider_operations(self) -> None:
        registry = PluginRegistry()
        provider = FakeProvider()
        registry.register(provider)
        self.assertEqual(("fake-provider",), tuple(item.plugin_id for item in registry.descriptors(kind="provider")))

    def test_registry_scopes_duplicate_ids_by_kind_and_rejects_same_kind(self) -> None:
        registry = PluginRegistry()
        registry.register(FakeSystemProbe())
        # The same textual id is valid in another kind because the key is scoped.
        same_id_runtime = FakeRuntime()
        object.__setattr__(same_id_runtime, "descriptor", PluginDescriptor("fake-system", "runtime"))
        registry.register(same_id_runtime)
        with self.assertRaises(PluginRegistryError):
            registry.register(FakeSystemProbe())

    def test_incomplete_plugin_report_is_actionable(self) -> None:
        report = conformance_report(IncompletePlugin())
        self.assertFalse(report.ok)
        self.assertIn("missing_method", {issue.code for issue in report.issues})
        with self.assertRaises(PluginRegistryError):
            PluginRegistry().register(IncompletePlugin())

    def test_register_many_isolates_bad_plugin(self) -> None:
        registry = PluginRegistry()
        reports = registry.register_many([FakeProvider(), IncompletePlugin(), FakeValidator()])
        self.assertEqual((True, False, True), tuple(report.ok for report in reports))
        self.assertEqual(
            {"fake-provider", "fake-validator"},
            {item.plugin_id for item in registry.descriptors()},
        )

    def test_invoke_converts_one_plugin_crash_to_local_failure(self) -> None:
        registry = PluginRegistry()
        registry.register(CrashingProvider())
        result = registry.invoke(
            "provider",
            "crashing-provider",
            "discover_catalog",
            DiscoveryRequest(),
        )
        self.assertFalse(result.ok)
        self.assertIn("RuntimeError", result.error or "")

        class ProviderWithSecretError(CrashingProvider):
            descriptor = PluginDescriptor("secret-provider", "provider")

            def discover_catalog(self, request: DiscoveryRequest) -> Evidence:
                raise RuntimeError("authorization: Bearer super-secret-token")

        registry.register(ProviderWithSecretError())
        redacted = registry.invoke(
            "provider", "secret-provider", "discover_catalog", DiscoveryRequest()
        )
        self.assertNotIn("super-secret-token", redacted.error or "")
        self.assertIn("<redacted>", redacted.error or "")

    def test_invoke_rejects_operations_outside_kind_contract(self) -> None:
        class ExtraProbe(FakeSystemProbe):
            def private_helper(self, request: ProbeRequest) -> Evidence:
                return Evidence("ready")

        registry = PluginRegistry()
        registry.register(ExtraProbe())
        with self.assertRaises(PluginRegistryError):
            registry.invoke("system_probe", "fake-system", "private_helper", ProbeRequest())

    def test_invalid_descriptor_is_rejected_without_provider_contact(self) -> None:
        class BadId:
            descriptor = PluginDescriptor("../escape", "system_probe")

            def probe(self, request: ProbeRequest) -> Evidence:
                raise AssertionError("must not be called during conformance")

        report = conformance_report(BadId())
        self.assertFalse(report.ok)
        self.assertIn("invalid_plugin_id", {issue.code for issue in report.issues})

    def test_evidence_unknown_is_not_ready(self) -> None:
        quota = FakeProvider().discover_quota(DiscoveryRequest())
        self.assertEqual("unknown", quota.status)
        self.assertNotEqual("ready", quota.status)


if __name__ == "__main__":
    unittest.main()
