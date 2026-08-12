from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sqlite_controller.py"
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
from sqlite_controller import SQLiteController  # noqa: E402
from sqlite_store import SQLiteStore  # noqa: E402


class SQLiteControllerTests(unittest.TestCase):
    def test_long_running_lane_keeps_controller_lease_heartbeat_alive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            artifact = workspace / "heartbeat.txt"
            controller = SQLiteController(
                root / "heartbeat.sqlite3",
                workspace=workspace,
                heartbeat_interval_seconds=0.1,
            )
            packet = {
                "schema_version": 1,
                "packet_id": "packet-heartbeat",
                "job_id": "heartbeat-job",
                "workspace": str(workspace),
                "write_scope": "out/heartbeat",
                "required_artifacts": [str(artifact)],
                "validation_required": True,
                "validation_argv": [sys.executable, "-c", "import sys; sys.exit(0)"],
                "attempts": [{
                    "attempt_id": "heartbeat-attempt",
                    "adapter": "command",
                    "transport": "local",
                    "argv": [
                        sys.executable,
                        "-c",
                        "import pathlib,time; time.sleep(1.2); pathlib.Path('heartbeat.txt').write_text('ok\\n')",
                    ],
                    "model": "local/fake",
                    "pool_id": "fake.local",
                    "provider": "fake",
                }],
            }
            controller.enqueue(packet)
            result: dict[str, object] = {}

            def run() -> None:
                result.update(controller.run(once=True, owner_id="heartbeat-owner"))

            thread = threading.Thread(target=run)
            thread.start()
            db = root / "heartbeat.sqlite3"
            deadline = time.time() + 5
            first_heartbeat = None
            while time.time() < deadline and first_heartbeat is None:
                if db.exists():
                    with SQLiteStore(db) as store:
                        leases = store.snapshot()["leases"]
                        if leases:
                            first_heartbeat = leases[0]["heartbeat_at_utc"]
                if first_heartbeat is None:
                    time.sleep(0.02)
            self.assertIsNotNone(first_heartbeat)
            with SQLiteStore(db) as store:
                first_job_lease = None
                first_attempt_lease = None
                deadline = time.time() + 5
                while time.time() < deadline:
                    snapshot = store.snapshot()
                    if snapshot["jobs"] and snapshot["attempts"]:
                        first_job_lease = snapshot["jobs"][0]["lease_expires_at_utc"]
                        first_attempt_lease = snapshot["attempts"][0]["lease_expires_at_utc"]
                        break
                    time.sleep(0.02)
                self.assertIsNotNone(first_job_lease)
                self.assertIsNotNone(first_attempt_lease)
            time.sleep(0.35)
            with SQLiteStore(db) as store:
                snapshot = store.snapshot()
                current = snapshot["leases"][0]
            self.assertNotEqual(first_heartbeat, current["heartbeat_at_utc"])
            self.assertEqual("active", current["status"])
            self.assertNotEqual(first_job_lease, snapshot["jobs"][0]["lease_expires_at_utc"])
            self.assertNotEqual(first_attempt_lease, snapshot["attempts"][0]["lease_expires_at_utc"])
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertEqual("completed", result["results"][0]["status"])

    def test_run_claims_and_completes_two_independent_lanes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            controller = SQLiteController(root / "dispatch.sqlite3", workspace=workspace)
            for index in range(2):
                name = f"lane-{index}.txt"
                packet = {
                    "schema_version": 1,
                    "packet_id": f"packet-{index}",
                    "job_id": f"lane-{index}",
                    "workspace": str(workspace),
                    "write_scope": f"out/{index}",
                    "required_artifacts": [f"out/{name}"],
                    "validation_required": True,
                    "validation_argv": [
                        sys.executable,
                        "-c",
                        f"import pathlib,sys; sys.exit(0 if pathlib.Path('out/{name}').is_file() else 2)",
                    ],
                    "attempts": [{
                        "attempt_id": f"attempt-{index}",
                        "adapter": "command",
                        "transport": "local",
                        "argv": [
                            sys.executable,
                            "-c",
                            f"import pathlib,time; time.sleep(.1); pathlib.Path('out/{name}').parent.mkdir(exist_ok=True); pathlib.Path('out/{name}').write_text('ok\\n')",
                        ],
                        "model": "gpt-5.3-codex-spark",
                        "pool_id": "codex.spark",
                        "provider": "codex",
                    }],
                }
                controller.enqueue(packet)
            result = controller.run(once=True, max_lanes=2)
            self.assertEqual(2, len(result["results"]))
            self.assertEqual({"completed"}, {row["status"] for row in result["results"]})
            self.assertTrue((workspace / "out/lane-0.txt").is_file())
            self.assertTrue((workspace / "out/lane-1.txt").is_file())

    def test_transient_failure_is_backed_off_before_retry_lane_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            controller = SQLiteController(root / "backoff.sqlite3", workspace=workspace)
            packet = {
                "schema_version": 1,
                "packet_id": "packet-backoff",
                "job_id": "backoff-job",
                "workspace": str(workspace),
                "write_scope": "out/backoff",
                "required_artifacts": ["out/result.txt"],
                "validation_required": True,
                "validation_argv": [sys.executable, "-c", "import sys; sys.exit(0)"],
                "attempts": [
                    {
                        "attempt_id": "backoff-first",
                        "adapter": "command",
                        "transport": "local",
                        "argv": [
                            sys.executable,
                            "-c",
                            "print('connection timed out'); raise SystemExit(7)",
                        ],
                        "model": "local/fake",
                        "pool_id": "fake.local",
                        "provider": "fake",
                        "fallback_on": ["network"],
                        "retry_backoff_seconds": 30,
                    },
                    {
                        "attempt_id": "backoff-second",
                        "adapter": "command",
                        "transport": "local",
                        "argv": [
                            sys.executable,
                            "-c",
                            "import pathlib; pathlib.Path('out').mkdir(); pathlib.Path('out/result.txt').write_text('ok\\n')",
                        ],
                        "model": "local/fake",
                        "pool_id": "fake.local",
                        "provider": "fake",
                    },
                ],
            }
            controller.enqueue(packet)
            first = controller.run(once=True)
            self.assertEqual("retry", first["results"][0]["status"])
            self.assertGreaterEqual(first["results"][0]["retry_delay_seconds"], 30)
            immediate = controller.run(once=True)
            self.assertEqual([], immediate["results"])
            with SQLiteStore(root / "backoff.sqlite3") as store:
                self.assertEqual("retry", store.get_job("backoff-job")["status"])

    def test_one_lane_exception_is_terminal_and_does_not_drop_sibling(self):
        """A broken lane is recorded while an independent lane still finishes."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            controller = SQLiteController(root / "dispatch.sqlite3", workspace=workspace)

            def packet(job_id: str, filename: str) -> dict[str, object]:
                return {
                    "schema_version": 1,
                    "packet_id": f"packet-{job_id}",
                    "job_id": job_id,
                    "workspace": str(workspace),
                    "write_scope": f"out/{job_id}",
                    "required_artifacts": [f"out/{filename}"],
                    "validation_required": True,
                    "validation_argv": [
                        sys.executable,
                        "-c",
                        f"import pathlib,sys; sys.exit(0 if pathlib.Path('out/{filename}').is_file() else 7)",
                    ],
                    "attempts": [{
                        "attempt_id": f"attempt-{job_id}",
                        "adapter": "command",
                        "transport": "local",
                        "argv": [
                            sys.executable,
                            "-c",
                            f"import pathlib; pathlib.Path('out/{filename}').parent.mkdir(exist_ok=True); pathlib.Path('out/{filename}').write_text('ok\\n')",
                        ],
                        "model": "local/fake",
                        "pool_id": "fake.local",
                        "provider": "fake",
                    }],
                }

            controller.enqueue(packet("lane-fails", "fails.txt"))
            controller.enqueue(packet("lane-succeeds", "succeeds.txt"))
            original = controller._execute_claim

            def flaky(store, claim, owner_id, fence_token):
                if (claim.get("job") or {}).get("job_id") == "lane-fails":
                    raise RuntimeError("synthetic lane failure")
                return original(store, claim, owner_id, fence_token)

            controller._execute_claim = flaky
            result = controller.run(once=True, max_lanes=2)
            self.assertEqual(2, len(result["results"]))
            by_job = {row["job_id"]: row for row in result["results"]}
            self.assertEqual("failed", by_job["lane-fails"]["status"])
            self.assertEqual("controller", by_job["lane-fails"]["error_class"])
            self.assertEqual("completed", by_job["lane-succeeds"]["status"])
            self.assertTrue((workspace / "out/succeeds.txt").is_file())
            with SQLiteStore(root / "dispatch.sqlite3") as store:
                snapshot = store.snapshot()
            statuses = {row["job_id"]: row["status"] for row in snapshot["jobs"]}
            self.assertEqual({"lane-fails": "failed", "lane-succeeds": "completed"}, statuses)
            self.assertTrue(any(row["event_type"] == "job_failed" for row in snapshot["events"]))

    def test_enqueue_requeues_failed_job_from_an_approved_replan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            artifact = workspace / "out" / "replanned.txt"
            controller = SQLiteController(root / "dispatch.sqlite3", workspace=workspace)
            first = {
                "schema_version": 1,
                "packet_id": "packet-replan-first",
                "job_id": "replan-controller-job",
                "workspace": str(workspace),
                "write_scope": "out/replan-controller-job",
                "required_artifacts": ["out/replanned.txt"],
                "validation_required": True,
                "validation_argv": [sys.executable, "-c", "import sys; sys.exit(0)"],
                "attempts": [{
                    "attempt_id": "attempt-first",
                    "adapter": "command",
                    "transport": "local",
                    "argv": [sys.executable, "-c", "print('rate limit'); raise SystemExit(7)"],
                    "model": "gpt-5.3-codex-spark",
                    "pool_id": "codex.spark",
                    "provider": "codex",
                }],
            }
            controller.enqueue(first)
            failed = controller.run(once=True)
            self.assertEqual("failed", failed["results"][0]["status"])

            replanned = dict(first)
            replanned["packet_id"] = "packet-replan-second"
            replanned["attempts"] = [{
                **first["attempts"][0],
                "attempt_id": "attempt-second",
                "argv": [
                    sys.executable,
                    "-c",
                    "import pathlib; pathlib.Path('out').mkdir(); pathlib.Path('out/replanned.txt').write_text('ok\\n')",
                ],
            }]
            queued = controller.enqueue(replanned)
            self.assertEqual("queued", queued["status"])
            completed = controller.run(once=True)
            self.assertEqual("completed", completed["results"][0]["status"])
            self.assertTrue(artifact.is_file())
            with SQLiteStore(root / "dispatch.sqlite3") as store:
                job = store.get_job("replan-controller-job")
                self.assertEqual("completed", job["status"])
                self.assertEqual(2, job["attempt_count"])
                self.assertTrue(any(e["event_type"] == "job_requeued" for e in store.list_events("replan-controller-job")))

    def test_cli_enqueue_run_and_status_use_transactional_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            result = workspace / "result.txt"
            packet = {
                "schema_version": 1,
                "packet_id": "packet-sqlite-e2e",
                "job_id": "sqlite-e2e",
                "workspace": str(workspace),
                "write_scope": "artifacts/sqlite-e2e",
                "required_artifacts": [str(result)],
                "validation_required": True,
                "validation_argv": [sys.executable, "-c", "import pathlib,sys; sys.exit(0 if pathlib.Path('result.txt').read_text() == 'done\\n' else 7)"],
                "attempts": [{
                    "attempt_id": "packet-attempt",
                    "adapter": "command",
                    "transport": "local",
                    "argv": [sys.executable, "-c", "import pathlib; pathlib.Path('result.txt').write_text('done\\n')"],
                    "result_source_path": str(result),
                    "output_path": str(result),
                    "model": "gpt-5.3-codex-spark",
                    "pool_id": "codex.spark",
                    "provider": "codex",
                }],
            }
            packet_path = root / "packet.json"
            db_path = root / "dispatch.sqlite3"
            packet_path.write_text(json.dumps(packet), encoding="utf-8")

            enqueue = subprocess.run(
                [sys.executable, str(SCRIPT), "enqueue", "--db", str(db_path), "--job-file", str(packet_path)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(0, enqueue.returncode, enqueue.stdout + enqueue.stderr)
            run = subprocess.run(
                [sys.executable, str(SCRIPT), "run", "--db", str(db_path), "--workspace", str(workspace), "--once"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(0, run.returncode, run.stdout + run.stderr)
            run_payload = json.loads(run.stdout)
            self.assertEqual("sqlite", run_payload["backend"])
            self.assertEqual("completed", run_payload["results"][0]["status"])
            self.assertTrue(run_payload["results"][0]["validation"]["ok"])
            self.assertEqual("done\n", result.read_text(encoding="utf-8"))

            status = subprocess.run(
                [sys.executable, str(SCRIPT), "status", "--db", str(db_path)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(0, status.returncode, status.stdout + status.stderr)
            snapshot = json.loads(status.stdout)["snapshot"]
            self.assertEqual("completed", snapshot["jobs"][0]["status"])
            self.assertTrue(any(row["event_type"] == "job_completed" for row in snapshot["events"]))

    def test_cli_enqueue_enforces_modern_packet_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            packet_path = root / "bad-packet.json"
            db_path = root / "dispatch.sqlite3"
            packet_path.write_text(
                json.dumps({"schema_version": 1, "packet_id": "bad", "job_id": "bad"}),
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "enqueue", "--db", str(db_path), "--job-file", str(packet_path)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(2, result.returncode)
            self.assertIn("task packet", result.stdout)
            if db_path.exists():
                from sqlite_store import SQLiteStore
                with SQLiteStore(db_path) as store:
                    self.assertEqual([], store.snapshot()["jobs"])


if __name__ == "__main__":
    unittest.main()
