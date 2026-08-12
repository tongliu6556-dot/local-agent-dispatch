from __future__ import annotations

import json
import hashlib
import io
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import remote_worker as worker  # noqa: E402


def packet(root: pathlib.Path, *, job_id: str = "server-job") -> dict:
    return {
        "schema_version": 1,
        "packet_id": f"packet-{job_id}",
        "job_id": job_id,
        "workspace": str(root),
        "write_scope": "src",
        "required_artifacts": ["out/result.json"],
        "validation_required": True,
        "validation_argv": ["python3", "-m", "unittest"],
        "attempts": [
            {
                "attempt_id": "attempt-1",
                "adapter": "server_local",
                "transport": "local",
                "model": "local/fake",
                "prompt_file": "TASK.md",
                "argv": ["provider", "--model", "local/fake", "--prompt-file", "TASK.md"],
            }
        ],
    }


class RemoteWorkerTests(unittest.TestCase):
    def test_validate_and_prepare_redact_prompt_and_raw_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "src").mkdir()
            report = worker.validate_packet(packet(root), root)
            self.assertEqual("server-job", report["job_id"])
            self.assertNotIn("argv", json.dumps(report))
            self.assertNotIn("TASK.md", json.dumps(report))
            manifest = worker.prepare_job(packet(root), root / "spool", root)
            self.assertEqual("prepared", manifest["status"])
            persisted = (root / "spool" / "jobs" / "server-job" / "manifest.json").read_text()
            events = (root / "spool" / "jobs" / "server-job" / "events.jsonl").read_text()
            self.assertNotIn("argv", persisted)
            self.assertNotIn("TASK.md", persisted)
            self.assertNotIn("argv", events)

    def test_lease_heartbeat_and_expired_lease_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "src").mkdir()
            spool = root / "spool"
            worker.prepare_job(packet(root), spool, root)
            first = worker.claim_job(spool, "server-job", "worker-a", lease_seconds=1)
            self.assertEqual("running", first["status"])
            self.assertTrue(first["lease_token"])
            with self.assertRaises(worker.WorkerError):
                worker.claim_job(spool, "server-job", "worker-b", lease_seconds=1)
            refreshed = worker.heartbeat(spool, "server-job", "worker-a", first["lease_token"], lease_seconds=2)
            self.assertEqual("running", refreshed["status"])
            time.sleep(2.1)
            recovered = worker.recover_jobs(spool, "server-job")
            self.assertEqual("recoverable", recovered[0]["status"])
            self.assertIsNone(recovered[0]["lease"])
            second = worker.claim_job(spool, "server-job", "worker-b", lease_seconds=2)
            self.assertEqual("running", second["status"])
            with self.assertRaises(worker.WorkerError):
                worker.heartbeat(spool, "server-job", "worker-a", first["lease_token"])

    def test_atomic_resume_reconciles_expired_lease_and_emits_audit(self):
        """Reconnect gets one fenced recovery + handoff snapshot."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "src").mkdir()
            spool = root / "spool"
            worker.prepare_job(packet(root, job_id="atomic-resume"), spool, root)
            first = worker.claim_job(spool, "atomic-resume", "chat-worker", lease_seconds=1)
            expiry = worker._parse_time(first["lease"]["expires_at"])
            with patch.object(worker.time, "time", return_value=expiry + 1.0):
                handoff = worker.recover_and_handoff(spool, "atomic-resume")
            self.assertEqual("recoverable", handoff["status"])
            self.assertTrue(handoff["resume_allowed"])
            self.assertEqual("claim_with_new_owner", handoff["next_action"])
            self.assertEqual({"performed": True, "reason": "expired"}, handoff["recovery"])
            self.assertIsNone(handoff["lease"])
            self.assertIn("lease_recovered", [event["event_type"] for event in handoff["events"]])
            self.assertNotIn("lease_token", json.dumps(handoff))

    def test_provider_free_durable_cycle_recovery_artifact_gate_and_handoff(self):
        """Exercise prepare -> claim -> heartbeat -> recover -> resume -> complete."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "src").mkdir()
            spool = root / "spool"
            prepared = worker.prepare_job(packet(root), spool, root)
            first = worker.claim_job(spool, "server-job", "worker-a", lease_seconds=1)
            refreshed = worker.heartbeat(
                spool,
                "server-job",
                "worker-a",
                first["lease_token"],
                lease_seconds=2,
            )

            # Avoid a real sleep: recovery observes the same clock used by the
            # lease fence and therefore deterministically sees the lease expire.
            expiry = worker._parse_time(refreshed["lease"]["expires_at"])
            with patch.object(worker.time, "time", return_value=expiry + 1.0):
                recovered = worker.recover_jobs(spool, "server-job")
            self.assertEqual("recoverable", recovered[0]["status"])
            self.assertIsNone(recovered[0]["lease"])

            resumed = worker.claim_job(spool, "server-job", "worker-b", lease_seconds=90)
            with self.assertRaisesRegex(worker.WorkerError, "lease fence mismatch"):
                worker.heartbeat(spool, "server-job", "worker-a", first["lease_token"])

            result = worker.run_fake_job(
                spool,
                "server-job",
                "worker-b",
                lease_token=resumed["lease_token"],
                artifact_text="provider-free fixture\n",
            )
            self.assertEqual("completed", result["status"])
            self.assertEqual("completed", result["manifest"]["status"])
            self.assertTrue(result["manifest"]["artifact_freshness_verified"])
            artifact = root / "out/result.json"
            self.assertTrue(artifact.is_file())
            expected_digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            self.assertEqual(expected_digest, result["artifact_manifest"][0]["sha256"])
            self.assertIsNotNone(result["heartbeat"])

            handoff = worker.resume_handoff(spool, "server-job")
            self.assertEqual(prepared["packet_digest"], handoff["packet_digest"])
            self.assertFalse(handoff["resume_required"])
            self.assertFalse(handoff["resume_allowed"])
            self.assertEqual("review_artifacts", handoff["next_action"])
            event_types = [event["event_type"] for event in handoff["events"]]
            for event_type in ("prepared", "lease_acquired", "heartbeat", "lease_recovered", "job_completed"):
                self.assertIn(event_type, event_types)
            # Handoffs are safe to persist or pass to a later controller: the
            # original prompt/argv never crosses the boundary.
            self.assertNotIn("argv", json.dumps(handoff))
            self.assertNotIn("prompt", json.dumps(handoff))

    def test_fake_executor_and_resume_handoff_cli_are_local_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "src").mkdir()
            packet_path = root / "packet.json"
            packet_path.write_text(json.dumps(packet(root)), encoding="utf-8")
            spool = root / "spool"
            self.assertEqual(
                0,
                worker.main(
                    [
                        "prepare",
                        "--packet",
                        str(packet_path),
                        "--project-root",
                        str(root),
                        "--spool",
                        str(spool),
                    ]
                ),
            )
            # CLI output is intentionally ignored here; this assertion only
            # proves the bounded fake seam can be driven without a provider.
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    0,
                    worker.main(
                        [
                            "fake-execute",
                            "--spool",
                            str(spool),
                            "--job-id",
                            "server-job",
                            "--owner",
                            "ci-fake",
                        ]
                    ),
                )
            handoff_path = root / "handoff.json"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    0,
                    worker.main(
                        [
                            "resume-handoff",
                            "--spool",
                            str(spool),
                            "--job-id",
                            "server-job",
                            "--output",
                            str(handoff_path),
                        ]
                    ),
                )
            payload = json.loads(handoff_path.read_text(encoding="utf-8"))
            self.assertEqual("completed", payload["status"])
            self.assertTrue((root / "out/result.json").is_file())
            self.assertFalse((spool / "jobs" / "server-job" / "provider.log").exists())

    def test_artifact_manifest_hashes_files_and_confines_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "src").mkdir()
            (root / "out").mkdir()
            (root / "out/result.json").write_text('{"ok":true}\n')
            spool = root / "spool"
            worker.prepare_job(packet(root), spool, root)
            before = worker.status(spool, "server-job")["jobs"][0]["artifact_manifest"][0]
            self.assertEqual("present", before["status"])
            (root / "out/result.json").write_text('{"ok":false}\n')
            observed = worker.observe_artifacts(spool, "server-job")
            after = observed["observed_artifact_manifest"][0]
            self.assertEqual("present", after["status"])
            self.assertNotEqual(before["sha256"], after["sha256"])
            with self.assertRaisesRegex(worker.WorkerError, "lease.*required"):
                worker.record_artifacts(spool, "server-job")
            bad = packet(root, job_id="escape")
            bad["required_artifacts"] = ["../outside.txt"]
            with self.assertRaises(worker.WorkerError):
                worker.validate_packet(bad, root)

    def test_secret_or_inline_prompt_packets_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "src").mkdir()
            bad_secret = packet(root, job_id="secret")
            bad_secret["metadata"] = {"api_key": "do-not-store"}
            with self.assertRaises(worker.WorkerError):
                worker.validate_packet(bad_secret, root)
            bad_prompt = packet(root, job_id="prompt")
            bad_prompt["attempts"][0]["prompt"] = "private user request"
            with self.assertRaises(worker.WorkerError):
                worker.validate_packet(bad_prompt, root)

    def test_top_level_paths_are_confined_like_attempt_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "src").mkdir()
            bad = packet(root, job_id="top-level-escape")
            bad["output_path"] = "../outside.txt"
            with self.assertRaisesRegex(worker.WorkerError, "output_path escapes project root"):
                worker.validate_packet(bad, root)

            outside = pathlib.Path(tmp).parent / f"outside-{root.name}.txt"
            outside.write_text("outside\n", encoding="utf-8")
            try:
                link = root / "linked-output"
                link.symlink_to(outside)
                bad_link = packet(root, job_id="symlink-escape")
                bad_link["output_path"] = "linked-output"
                with self.assertRaisesRegex(worker.WorkerError, "output_path escapes project root"):
                    worker.validate_packet(bad_link, root)
            finally:
                outside.unlink(missing_ok=True)

    def test_short_lease_timestamp_keeps_subsecond_precision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "src").mkdir()
            spool = root / "spool"
            worker.prepare_job(packet(root, job_id="micro-lease"), spool, root)
            claimed = worker.claim_job(spool, "micro-lease", "worker-a", lease_seconds=1)
            self.assertIn(".", str(claimed["lease"]["expires_at"]))

    def test_cli_validate_prepare_status_and_recover_are_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "src").mkdir()
            packet_path = root / "packet.json"
            packet_path.write_text(json.dumps(packet(root)))
            self.assertEqual(0, worker.main(["validate", "--packet", str(packet_path), "--project-root", str(root)]))
            self.assertEqual(0, worker.main(["prepare", "--packet", str(packet_path), "--project-root", str(root), "--spool", str(root / "spool")]))
            self.assertEqual(0, worker.main(["status", "--spool", str(root / "spool")]))
            self.assertEqual(0, worker.main(["recover", "--spool", str(root / "spool")]))
            # The contract has no execute subcommand and should not create a
            # provider log, process, network request, or raw prompt artifact.
            self.assertFalse((root / "spool" / "jobs" / "server-job" / "provider.log").exists())

    def test_fake_service_is_chat_independent_and_recovers_named_job(self):
        """A separate process can continue one authorized fixture job."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "src").mkdir()
            spool = root / "spool"
            job = packet(root, job_id="service-job")
            prepared = worker.prepare_job(job, spool, root)
            self.assertEqual("prepared", prepared["status"])

            # Simulate the originating controller disappearing after its lease
            # was acquired.  The independent service must reconcile the lease
            # before claiming the same named job.
            first = worker.claim_job(spool, "service-job", "chat-worker", lease_seconds=1)
            expiry = worker._parse_time(first["lease"]["expires_at"])
            with patch.object(worker.time, "time", return_value=expiry + 1.0):
                recovered = worker.recover_jobs(spool, "service-job")
            self.assertEqual("recoverable", recovered[0]["status"])

            script = ROOT / "scripts" / "remote_worker.py"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "fake-service",
                    "--spool",
                    str(spool),
                    "--job-id",
                    "service-job",
                    "--owner",
                    "server-service",
                    "--poll-seconds",
                    "0.01",
                    "--max-idle-rounds",
                    "3",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual("fake", payload["service"])
            self.assertFalse(payload["provider_execution"])
            self.assertEqual("completed", payload["status"])
            self.assertEqual("completed", worker.status(spool, "service-job")["jobs"][0]["status"])
            self.assertTrue((root / "out/result.json").is_file())
            self.assertNotIn("prompt", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
