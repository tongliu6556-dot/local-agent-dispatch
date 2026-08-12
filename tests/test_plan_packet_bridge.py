from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import plan_packet_bridge as bridge  # noqa: E402
import continuity_controller as continuity  # noqa: E402


def base_inputs(root: pathlib.Path):
    workspace = root / "workspace"
    workspace.mkdir()
    (workspace / "task.md").write_text("bounded task", encoding="utf-8")
    state = {
        "schema_version": 1,
        "workspace": str(workspace),
        "hosts": {
            "local_mac": {
                "host_id": "local_mac",
                "transport": "local",
                "reachable": True,
            },
            "remote_gpu": {
                "host_id": "remote_gpu",
                "transport": "ssh",
                "reachable": True,
                "project_path": "/srv/project",
            },
        },
    }
    job = {
        "job_id": "job-1",
        "workspace": str(workspace),
        "prompt_file": str(workspace / "task.md"),
        "result_source_path": str(workspace / "result.txt"),
        "output_path": str(workspace / "result.txt"),
        "required_artifacts": [str(workspace / "result.txt")],
        "write_scope": "src",
        "validation_argv": [sys.executable, "-c", "import sys; sys.exit(0)"],
    }
    assignment = {
        "job_id": "job-1",
        "pool_id": "codex.spark",
        "model": "gpt-5.3-codex-spark",
        "variant": "xhigh",
        "execution_host": "local_mac",
        "execution_transport": "local",
        "workload_host": "local_mac",
        "workload_transport": "local",
        "write_scope": "src",
        "resource_request": {"cpu_cores": 1},
    }
    registry = {
        "codex.spark": {
            "provider": "codex",
            "adapter": "command",
            "transport": "local",
            "argv": ["python3", "-c", "print('{model}')"],
        }
    }
    return state, job, assignment, registry


class PlanPacketBridgeTests(unittest.TestCase):
    def test_valid_assignment_keeps_exact_model_and_safe_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, job, assignment, registry = base_inputs(pathlib.Path(tmp))
            packet = bridge.assignment_to_packet(
                assignment, job, state, registry, plan_digest="a" * 64
            )
            self.assertEqual("gpt-5.3-codex-spark", packet["model"])
            self.assertEqual("xhigh", packet["variant"])
            self.assertTrue(packet["validation_required"])
            self.assertNotIn("bounded task", repr(packet["attempts"][0]["argv"]))
            self.assertEqual(packet["model"], packet["attempts"][0]["model"])

    def test_split_desktop_workload_fails_closed_without_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, job, assignment, registry = base_inputs(pathlib.Path(tmp))
            assignment["workload_host"] = "remote_gpu"
            assignment["workload_transport"] = "ssh"
            with self.assertRaisesRegex(bridge.BridgeError, "split_placement_requires_remote_wrapper"):
                bridge.assignment_to_packet(
                    assignment, job, state, registry, plan_digest="b" * 64
                )

    def test_path_escape_and_missing_adapter_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, job, assignment, registry = base_inputs(pathlib.Path(tmp))
            job["prompt_file"] = str(pathlib.Path(job["workspace"]) / ".." / "outside.md")
            with self.assertRaisesRegex(bridge.BridgeError, "prompt_file: path escapes workspace"):
                bridge.assignment_to_packet(
                    assignment, job, state, registry, plan_digest="c" * 64
                )
            job["prompt_file"] = str(pathlib.Path(job["workspace"]) / "task.md")
            with self.assertRaisesRegex(bridge.BridgeError, "missing adapter contract"):
                bridge.assignment_to_packet(
                    assignment, job, state, {}, plan_digest="c" * 64
                )

    def test_bridge_report_is_dry_run_and_preserves_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, job, assignment, registry = base_inputs(pathlib.Path(tmp))
            plan = {
                "schema_version": 1,
                "ok": True,
                "decision": "dispatch",
                "assignments": [assignment, {"job_id": "unknown", "model": "x", "pool_id": "codex.spark"}],
            }
            report = bridge.bridge_plan(plan, [job], state, registry)
            self.assertFalse(report["ok"])
            self.assertTrue(report["read_only"])
            self.assertEqual(1, len(report["packets"]))
            self.assertEqual("unknown", report["errors"][0]["job_id"])

    def test_explicit_sqlite_enqueue_is_separate_from_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            state, job, assignment, registry = base_inputs(root)
            plan = {
                "schema_version": 1,
                "ok": True,
                "decision": "dispatch",
                "assignments": [assignment],
            }
            db = root / "dispatch.sqlite3"
            dry_run = bridge.bridge_plan(plan, [job], state, registry)
            self.assertTrue(dry_run["ok"])
            self.assertTrue(dry_run["read_only"])
            self.assertFalse(db.exists())

            ready = bridge.bridge_plan(plan, [job], state, registry, mode="enqueue-ready")
            self.assertTrue(ready["ok"])
            self.assertTrue(ready["read_only"])
            self.assertFalse(db.exists())

            result = bridge.enqueue_packets(ready, db)
            self.assertTrue(result["ok"])
            self.assertTrue(result["enqueue_performed"])
            self.assertFalse(result["provider_execution"])
            self.assertEqual(["job-1"], [row["job_id"] for row in result["jobs"]])
            self.assertTrue(db.is_file())

            # The audit response is intentionally a summary, not a copy of
            # packet attempts, prompt paths, or command argv.
            self.assertNotIn("argv", repr(result))
            self.assertNotIn("bounded task", repr(result))

    def test_server_local_ssh_packet_uses_remote_workspace_and_host_validator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            workspace = root / "controller-workspace"
            workspace.mkdir()
            state = {
                "schema_version": 1,
                "workspace": str(workspace),
                "hosts": {
                    "remote-a": {
                        "host_id": "remote-a",
                        "transport": "ssh",
                        "reachable": True,
                        "project_path": "/srv/local-agent-dispatch",
                    }
                },
            }
            job = {
                "job_id": "remote-job",
                "workspace": str(workspace),
                "remote_workspace": "/srv/local-agent-dispatch/remote-job",
                "remote_prompt_file": "TASK.md",
                "remote_required_artifacts": ["out/result.txt"],
                "remote_result_source_path": "out/result.txt",
                "write_scope": "src",
                "validation_argv": ["python3", "-m", "unittest"],
            }
            assignment = {
                "job_id": "remote-job",
                "pool_id": "server_local.remote-a",
                "model": "qwen2.5-coder-14b-awq",
                "variant": None,
                "execution_host": "remote-a",
                "execution_transport": "ssh",
                "workload_host": "remote-a",
                "workload_transport": "ssh",
                "write_scope": "src",
            }
            registry = {
                "server_local.remote-a": {
                    "provider": "server_local",
                    "adapter": "server_local",
                    "transport": "ssh",
                    "argv": [
                        "/opt/venvs/aider/bin/aider",
                        "--model", "openai/{model}",
                        "--message-file", "{workspace}/TASK.md",
                    ],
                }
            }
            packet = bridge.assignment_to_packet(
                assignment, job, state, registry, plan_digest="d" * 64
            )
            self.assertEqual("/srv/local-agent-dispatch/remote-job", packet["workspace"])
            self.assertEqual(["/srv/local-agent-dispatch/remote-job/out/result.txt"], packet["required_artifacts"])
            self.assertEqual("python3", packet["validation_argv"][0])
            attempt = packet["attempts"][0]
            self.assertEqual("ssh", attempt["transport"])
            self.assertEqual("/srv/local-agent-dispatch/remote-job", attempt["workspace"])
            self.assertIn("/srv/local-agent-dispatch/remote-job/TASK.md", attempt["argv"])
            self.assertIsNone(attempt["output_path"])

    def test_server_local_ssh_rejects_local_absolute_validator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            state, job, assignment, _ = base_inputs(pathlib.Path(tmp))
            state["hosts"] = {
                "remote": {
                    "host_id": "remote", "transport": "ssh", "reachable": True,
                    "project_path": "/srv/project",
                }
            }
            job.update(
                remote_workspace="/srv/project/job",
                remote_prompt_file="TASK.md",
                remote_required_artifacts=["out/result.txt"],
                remote_result_source_path="out/result.txt",
                validation_argv=[sys.executable, "-m", "unittest"],
            )
            assignment.update(
                pool_id="server_local.remote", model="qwen2.5-coder-14b-awq",
                execution_host="remote", workload_host="remote",
                execution_transport="ssh", workload_transport="ssh",
            )
            registry = {
                "server_local.remote": {
                    "provider": "server_local", "adapter": "server_local", "transport": "ssh",
                    "argv": ["aider", "--model", "{model}"],
                }
            }
            with self.assertRaisesRegex(bridge.BridgeError, "remote validation"):
                bridge.assignment_to_packet(
                    assignment, job, state, registry, plan_digest="e" * 64
                )

    def test_server_openai_ssh_keeps_local_prompt_and_fences_remote_artifacts(self):
        """The controller prompt stays local while the result lives on SSH host."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            workspace = root / "controller-workspace"
            workspace.mkdir()
            prompt = workspace / "task.md"
            prompt.write_text("remote loopback task", encoding="utf-8")
            state = {
                "schema_version": 1,
                "workspace": str(workspace),
                "hosts": {
                    "remote-a": {
                        "host_id": "remote-a",
                        "transport": "ssh",
                        "reachable": True,
                        "project_path": "/srv/project",
                        "hostname": "remote-a.example.test",
                        "port": 22,
                        "user": "runner",
                    }
                },
            }
            job = {
                "job_id": "remote-openai-job",
                "workspace": str(workspace),
                "prompt_file": str(prompt),
                "remote_workspace": "/srv/project/job",
                "remote_required_artifacts": ["out/result.txt"],
                "remote_result_source_path": "out/result.txt",
                "write_scope": "out/remote-openai-job",
                "validation_argv": ["python3", "-c", "print('validate')"],
            }
            assignment = {
                "job_id": "remote-openai-job",
                "pool_id": "server_openai.remote-a",
                "model": "qwen2.5-coder-14b-awq",
                "variant": None,
                "execution_host": "remote-a",
                "execution_transport": "ssh",
                "workload_host": "remote-a",
                "workload_transport": "ssh",
                "write_scope": "out/remote-openai-job",
            }
            registry = {
                "server_openai.remote-a": {
                    "provider": "server_openai",
                    "adapter": "server_openai",
                    "transport": "ssh",
                    "base_url": "http://127.0.0.1:8000/v1",
                }
            }
            packet = bridge.assignment_to_packet(
                assignment, job, state, registry, plan_digest="f" * 64
            )
            self.assertEqual("strict", continuity.validate_task_packet(packet)["mode"])
            self.assertTrue(packet["workspace"].startswith(str(workspace.resolve())))
            self.assertEqual("/srv/project/job", packet["remote_workspace"])
            self.assertEqual(["/srv/project/job/out/result.txt"], packet["required_artifacts"])
            attempt = packet["attempts"][0]
            self.assertEqual(str(prompt.resolve()), attempt["prompt_file"])
            self.assertEqual("/srv/project/job", attempt["remote_workspace"])
            self.assertEqual("/srv/project/job/out/result.txt", attempt["remote_result_source_path"])
            self.assertIsNone(attempt["output_path"])
            argv, cwd, output_path, stdin_payload = continuity.build_attempt(
                job, attempt, state, state["hosts"]
            )
            self.assertTrue(argv[-1] == "python3 -")
            self.assertIsNone(cwd)
            self.assertIsNone(output_path)
            self.assertIn("/srv/project/job/out/result.txt", stdin_payload or "")


if __name__ == "__main__":
    unittest.main()
