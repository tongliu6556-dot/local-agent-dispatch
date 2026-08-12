#!/usr/bin/env python3
"""Unit tests for the read-only OpenCode Go discovery snapshot."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import unittest


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "opencode_go_snapshot.py"
SPEC = importlib.util.spec_from_file_location("opencode_go_snapshot", SCRIPT)
assert SPEC and SPEC.loader
snapshot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(snapshot)


class AuthParserTests(unittest.TestCase):
    def test_detects_go_provider_without_returning_credential_material(self) -> None:
        raw = (
            "\x1b[0m\n┌ Credentials ~/.local/share/opencode/auth.json\n"
            "● OpenCode Go \x1b[90mapi\x1b[0m\n"
            "token=sk-should-never-be-returned\n"
        )
        parsed = snapshot.parse_auth_provider(raw)
        self.assertEqual(parsed["state"], "configured")
        self.assertEqual(parsed["credential_type"], "api")
        rendered = json.dumps(parsed)
        self.assertNotIn("sk-should-never-be-returned", rendered)
        self.assertNotIn("auth.json", rendered)
        self.assertFalse(parsed["credential_values_inspected"])


class CatalogParserTests(unittest.TestCase):
    def test_parses_verbose_metadata_and_omits_sensitive_fields(self) -> None:
        raw = """opencode-go/gpt-5.6-luna
{
  "id": "gpt-5.6-luna",
  "providerID": "opencode-go",
  "name": "GPT-5.6 Luna (2x usage)",
  "family": "gpt-luna",
  "api": {"id": "gpt-5.6-luna", "url": "https://example.invalid/v1"},
  "headers": {"Authorization": "Bearer secret-value"},
  "options": {"apiKey": "another-secret"},
  "cost": {"input": 0.1, "output": 0.6},
  "limit": {"context": 1050000, "output": 128000},
  "capabilities": {"reasoning": true, "toolcall": true},
  "variants": {"max": {"reasoningEffort": "max", "budgetTokens": 31999}}
}
opencode-go/mimo-v2.5
{
  "id": "mimo-v2.5",
  "providerID": "opencode-go",
  "name": "MiMo V2.5",
  "status": "active",
  "limit": {"context": 1000000}
}
"""
        parsed = snapshot.parse_verbose_catalog(raw)
        self.assertEqual(parsed["state"], "visible")
        self.assertEqual(parsed["model_count"], 2)
        self.assertEqual(
            [row["model_id"] for row in parsed["models"]],
            ["opencode-go/gpt-5.6-luna", "opencode-go/mimo-v2.5"],
        )
        luna = parsed["models"][0]
        self.assertEqual(luna["metadata"]["variants"]["max"]["reasoningEffort"], "max")
        self.assertEqual(luna["metadata"]["variants"]["max"]["budgetTokens"], 31999)
        self.assertEqual(luna["runtime_state"], "unknown")
        self.assertEqual(luna["omitted_sensitive_metadata_fields"], ["headers", "options"])
        rendered = json.dumps(parsed)
        self.assertNotIn("secret-value", rendered)
        self.assertNotIn("another-secret", rendered)


class StatsParserTests(unittest.TestCase):
    def test_parses_local_history_without_calling_it_quota(self) -> None:
        raw = """
│ OVERVIEW │
│Sessions 2 │
│Messages 10 │
│Days 30 │
│ COST & TOKENS │
│Total Cost $0.00 │
│Avg Cost/Day $0.00 │
│Avg Tokens/Session 41.3K │
│Median Tokens/Session 41.3K │
│Input 37.3K │
│Output 291 │
│Cache Read 44.8K │
│Cache Write 0 │
│ MODEL USAGE │
│ opencode-go/mimo-v2.5 │
│ Messages 1 │
│ Input Tokens 0 │
│ Output Tokens 0 │
│ Cache Read 0 │
│ Cache Write 0 │
│ Cost $0.0000 │
│ TOOL USAGE │
"""
        parsed = snapshot.parse_local_stats(raw)
        self.assertEqual(parsed["overview"]["sessions"], 2)
        self.assertEqual(parsed["cost_and_tokens"]["input_tokens_approx"], 37_300)
        self.assertEqual(parsed["opencode_go_models"][0]["messages"], 1)
        self.assertIn("not evidence", parsed["note"])


class SnapshotSemanticsTests(unittest.TestCase):
    @staticmethod
    def command(stdout: str, ok: bool = True) -> dict:
        return {
            "ok": ok,
            "returncode": 0 if ok else 1,
            "stdout": stdout,
            "stderr": "",
            "timed_out": False,
        }

    def test_builds_one_shared_pool_with_unknown_runtime_and_quota(self) -> None:
        catalog = """opencode-go/example
{"id":"example","providerID":"opencode-go","name":"Example","status":"active"}
"""
        result = snapshot.build_snapshot(
            "/usr/local/bin/opencode",
            self.command("1.18.15\n"),
            self.command("● OpenCode Go api\n"),
            self.command(catalog),
            self.command("│ OVERVIEW │\n│Sessions 0 │\n│Messages 0 │\n│Days 30 │\n"),
        )
        self.assertEqual(list(result["pools"]), ["opencode.go"])
        pool = result["pools"]["opencode.go"]
        self.assertEqual(pool["availability_state"], "catalog_visible_runtime_unknown")
        self.assertEqual(pool["runtime_state"], "unknown")
        self.assertEqual(pool["quota"]["state"], "unknown")
        for window in ("five_hour", "weekly", "monthly"):
            self.assertIsNone(pool["quota"][window]["remaining_percent"])
        self.assertEqual(pool["shared_members"], ["opencode-go/example"])
        self.assertFalse(result["security"]["model_prompt_sent"])

    def test_failed_provider_probe_does_not_infer_missing_auth(self) -> None:
        result = snapshot.build_snapshot(
            "opencode",
            self.command("1.18.15"),
            self.command("", ok=False),
            self.command("", ok=False),
            self.command("", ok=False),
        )
        self.assertEqual(result["auth"]["state"], "unknown")
        self.assertEqual(result["catalog"]["state"], "unknown")
        self.assertFalse(result["ok"])

    def test_optional_stats_failure_does_not_erase_core_discovery(self) -> None:
        catalog = """opencode-go/example
{"id":"example","providerID":"opencode-go","name":"Example","status":"active"}
"""
        result = snapshot.build_snapshot(
            "/usr/local/bin/opencode",
            self.command("1.18.15\n"),
            self.command("● OpenCode Go api\n"),
            self.command(catalog),
            self.command("", ok=False),
        )
        self.assertTrue(result["ok"])
        self.assertEqual("visible", result["catalog"]["state"])
        self.assertEqual("unknown", result["stats"]["state"])


if __name__ == "__main__":
    unittest.main()
