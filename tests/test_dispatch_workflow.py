from __future__ import annotations

import pathlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import dispatch_workflow as workflow  # noqa: E402
import dispatch_schema  # noqa: E402


def _host(host_id: str, transport: str = "local") -> dict:
    return {
        "host_id": host_id,
        "transport": transport,
        "reachable": True,
        "project_path_exists": True,
        "project_path_writable": True,
        "logical_cpu_cores": 16 if transport == "local" else 64,
        "estimated_idle_cpu_cores": 12 if transport == "local" else 60,
        "memory_total_gib": 32 if transport == "local" else 128,
        "memory_available_gib": 24 if transport == "local" else 96,
        "disk_total_gib": 200 if transport == "local" else 500,
        "disk_free_gib": 80 if transport == "local" else 300,
        "gpu_count": 0,
        "gpus": [],
        "commands": {"python3": sys.executable},
        "python": {"version": "3.14"},
        "tags": ["local"] if transport == "local" else ["remote", "direct-link"],
    }


def _resources() -> dict:
    return {
        "input_gib": 0,
        "download_gib": 0,
        "environment_gib": 0,
        "temporary_gib": 0,
        "cache_gib": 0,
        "output_gib": 0,
        "cpu_cores": 1,
        "ram_gib": 1,
        "gpu_count": 0,
        "vram_gib": 0,
        "compute_minutes": 1,
    }


def _local_system() -> dict:
    return {
        "ok": True,
        "schema_version": 1,
        "os": {"name": "Darwin"},
        "arch": "arm64",
        "cpu": {"logical_cores": 10, "physical_cores": 10},
        "ram": {"total_gib": 32, "available_gib": 24},
        "disks": {
            "workspace": {
                "exists": True,
                "writable": True,
                "total_bytes": 200 * 1024**3,
                "free_bytes": 80 * 1024**3,
            }
        },
        "accelerators": [],
        "python": {"executable": sys.executable, "version": "3.14"},
        "clis": {},
    }


def _preflight() -> dict:
    return {
        "ok": True,
        "schema_version": 1,
        "scanned_at_utc": "2026-08-12T00:00:00+00:00",
        "compute_hosts": {
            "local_system": _host("local_system"),
            "remote_gpu": _host("remote_gpu", "ssh"),
        },
        "pools": {
            "antigravity.gemini": {
                "provider": "antigravity",
                "health": "ready",
                "effective_remaining_percent": 80,
                "reserve_percent": 10,
                "default_model": "gemini-3.1-pro-high",
                "catalog_models": ["gemini-3.1-pro-high"],
                "role_models": {"hard": "gemini-3.1-pro-high"},
                "role_model_candidates": {"hard": ["gemini-3.1-pro-high"]},
                "max_concurrency": 1,
                "inflight": 0,
            },
            "opencode.go": {
                "provider": "opencode",
                "health": "ready",
                "effective_remaining_percent": 80,
                "reserve_percent": 10,
                "default_model": "opencode-go/deepseek-v4-flash",
                "catalog_models": [],
                "policy_excluded_models": ["opencode-go/deepseek-v4-flash"],
                "available_model_variants": {"opencode-go/deepseek-v4-flash": ["max"]},
                "role_model_candidates": {},
                "max_concurrency": 1,
                "inflight": 0,
            },
        },
    }


class DispatchWorkflowTests(unittest.TestCase):
    def test_capture_model_allowlist_is_enforced_after_planner(self):
        with tempfile.TemporaryDirectory() as temporary:
            jobs = workflow._jobs_payload(
                {
                    "capture": "bounded-task-capture",
                    "policy": {"allowed_models": ["model-that-is-not-in-pool"]},
                    "planner_jobs": [
                        {
                            "job_id": "captured-audit",
                            "task_type": "audit",
                            "difficulty": 4,
                            "allowed_pools": ["antigravity.gemini"],
                            "resources": _resources(),
                        }
                    ],
                }
            )
            report = workflow.build_report(
                _local_system(), _preflight(), jobs, pathlib.Path(temporary), max_lanes=1
            )
        self.assertFalse(report["ok"])
        self.assertEqual([], report["assignments"])
        self.assertEqual("model_policy_violation", report["gates"]["model_policy"][0]["reason"])

    def test_captured_unknown_estimate_clears_stale_resource_hint(self):
        job = {
            "job_id": "captured-unknown",
            "capture_source": "captured-task",
            "resource_estimate": {"ram_gib": 256, "compute_minutes": 90},
        }
        with patch.object(
            workflow.task_estimator,
            "build_report",
            return_value={"estimate": {"pilot_required": False, "metrics": {}}},
        ):
            _reports, planner_jobs, gates = workflow._task_estimates([job], manifest=None, history=None)
        self.assertEqual([], gates)
        self.assertNotIn("resource_estimate", planner_jobs[0])

    def test_saved_preflight_is_provider_free_and_plans_two_lanes(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = pathlib.Path(temporary)
            jobs = [
                {
                    "job_id": "gemini-review",
                    "task_type": "audit",
                    "difficulty": 4,
                    "allowed_pools": ["antigravity.gemini"],
                    "resources": _resources(),
                },
                {
                    "job_id": "deepseek-review",
                    "task_type": "code",
                    "difficulty": 3,
                    "allowed_pools": ["opencode.go"],
                    "model_by_pool": {
                        "opencode.go": {
                            "model": "opencode-go/deepseek-v4-flash",
                            "variant": "max",
                        }
                    },
                    "allow_policy_excluded_models": ["opencode-go/deepseek-v4-flash"],
                    "resources": _resources(),
                },
                {"job_id": "needs-pilot", "task_type": "research"},
            ]
            report = workflow.run_workflow(
                workspace=workspace,
                jobs=jobs,
                preflight=_preflight(),
                system_snapshot=_local_system(),
                max_lanes=2,
                horizon=3,
                runner=lambda *args, **kwargs: (_ for _ in ()).throw(
                    AssertionError("saved preflight must not invoke discovery")
                ),
            )

        self.assertTrue(report["ok"])
        self.assertTrue(report["read_only"])
        self.assertFalse(report["provider_execution"])
        self.assertFalse(report["model_prompts_sent"])
        self.assertEqual(
            ["system_scan", "preflight", "task_estimate", "hardware_fit", "planner"],
            [stage["stage"] for stage in report["sequence"]],
        )
        self.assertTrue(report["multi_lane"]["parallel_wave"])
        self.assertEqual(2, report["multi_lane"]["planned_lane_count"])
        self.assertEqual(
            "gemini-3.1-pro-high",
            report["pools"]["antigravity.gemini"]["role_models"]["hard"],
        )
        self.assertEqual(
            ["opencode-go/deepseek-v4-flash"],
            report["pools"]["opencode.go"]["policy_excluded_models"],
        )
        assignments = {row["job_id"]: row for row in report["assignments"]}
        self.assertEqual("gemini-3.1-pro-high", assignments["gemini-review"]["model"])
        self.assertEqual("opencode-go/deepseek-v4-flash", assignments["deepseek-review"]["model"])
        self.assertEqual("max", assignments["deepseek-review"]["variant"])
        self.assertTrue(any(row["job_id"] == "needs-pilot" for row in report["gates"]["task_pilot"]))
        self.assertIn("needs-pilot", {row["job_id"] for row in report["planner"]["deferred"]})
        dispatch_schema.validate("dispatch_workflow_report", report)

    def test_build_report_exposes_remote_storage_and_pool_gates(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = workflow.build_report(
                local_system=_local_system(),
                preflight={
                    **_preflight(),
                    "pools": {
                        "antigravity.gemini": {
                            **_preflight()["pools"]["antigravity.gemini"],
                            "health": "unknown",
                            "effective_remaining_percent": None,
                            "unknown_quota_policy": "pilot",
                            "unknown_quota_pilot_percent": 5,
                        }
                    },
                },
                jobs=[{"job_id": "unknown-job", "task_type": "research"}],
                workspace=pathlib.Path(temporary),
                max_lanes=4,
            )
        self.assertIn("remote_gpu", report["hosts"])
        self.assertEqual("ssh", report["hosts"]["remote_gpu"]["transport"])
        self.assertIn("antigravity.gemini", report["gates"]["unknown_quota_pools"])
        self.assertIn("unknown-job", {row["job_id"] for row in report["gates"]["task_pilot"]})
        self.assertFalse(report["model_prompts_sent"])

    def test_lad_dispatch_reads_saved_snapshot_without_provider_contact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            jobs = root / "jobs.json"
            jobs.write_text(json.dumps({"jobs": [{"job_id": "offline-review"}]}), encoding="utf-8")
            preflight = root / "preflight.json"
            preflight.write_text(json.dumps({"ok": True, "compute_hosts": {}, "pools": {}}), encoding="utf-8")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "src")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "local_agent_dispatch.cli",
                    "dispatch",
                    "--workspace",
                    str(workspace),
                    "--jobs",
                    str(jobs),
                    "--preflight",
                    str(preflight),
                    "--max-lanes",
                    "2",
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("dispatch", payload["command"])
        self.assertEqual(1, payload["schema_version"])
        self.assertTrue(payload["read_only"])
        self.assertFalse(payload["provider_execution"])
        self.assertFalse(payload["model_prompts_sent"])


if __name__ == "__main__":
    unittest.main()
