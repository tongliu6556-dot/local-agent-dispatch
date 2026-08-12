from __future__ import annotations

import json
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
import importlib.util
import sys


_SPEC = importlib.util.spec_from_file_location(
    "sqlite_store_under_test", ROOT / "scripts" / "sqlite_store.py"
)
assert _SPEC and _SPEC.loader
sqlite_store = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = sqlite_store
_SPEC.loader.exec_module(sqlite_store)


class SQLiteStoreTests(unittest.TestCase):
    def open_store(self, root: pathlib.Path):
        return sqlite_store.SQLiteStore(root / "dispatch.sqlite3", timeout_seconds=5)

    def test_versioned_wal_schema_is_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            with self.open_store(root) as store:
                self.assertEqual(3, store.schema_version)
                tables = {
                    row[0]
                    for row in store.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertTrue({"schema_migrations", "jobs", "attempts", "events", "leases", "reservations"} <= tables)
                self.assertEqual("wal", str(store.connection.execute("PRAGMA journal_mode").fetchone()[0]).lower())
                migration = store.connection.execute(
                    "SELECT version, checksum FROM schema_migrations"
                ).fetchall()
                self.assertEqual([1, 2, 3], [int(row[0]) for row in migration])

    def test_controller_lease_fence_increments_and_rejects_stale_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            with self.open_store(root) as store:
                first = store.acquire_controller_lease("controller-a", ttl_seconds=10)
                self.assertEqual(1, first["schema_version"])
                with self.assertRaises(sqlite_store.LeaseConflict):
                    store.acquire_controller_lease("controller-b", ttl_seconds=10)
                store.release_controller_lease("controller-a", first["fence_token"])
                second = store.acquire_controller_lease("controller-b", ttl_seconds=10)
                self.assertGreater(second["fence_token"], first["fence_token"])
                with self.assertRaises(sqlite_store.FencingError):
                    store.heartbeat_controller_lease(
                        "controller-a", first["fence_token"], ttl_seconds=10
                    )

    def test_atomic_claim_only_one_worker_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            path = root / "dispatch.sqlite3"
            first = sqlite_store.SQLiteStore(path)
            second = sqlite_store.SQLiteStore(path)
            try:
                lease = first.acquire_controller_lease("controller", ttl_seconds=10)
                first.create_job("job-1", {"prompt": "fake", "model": "spark"})
                barrier = threading.Barrier(2)
                results: list[dict | None] = []

                def claim(store):
                    barrier.wait(timeout=5)
                    results.append(
                        store.claim_next_job(
                            "controller", lease["fence_token"], lease_ttl_seconds=10
                        )
                    )

                threads = [threading.Thread(target=claim, args=(first,)), threading.Thread(target=claim, args=(second,))]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)
                self.assertEqual(2, len(results))
                self.assertEqual(1, sum(item is not None for item in results))
                attempts = first.list_attempts("job-1")
                self.assertEqual(1, len(attempts))
                self.assertEqual(1, attempts[0]["schema_version"])
                self.assertEqual("running", attempts[0]["status"])
            finally:
                first.close()
                second.close()

    def test_claim_jobs_reserves_multiple_lanes_in_one_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            with self.open_store(root) as store:
                lease = store.acquire_controller_lease("controller", ttl_seconds=10)
                for index in range(4):
                    store.create_job(f"lane-{index}", {"lane": index}, priority=index)
                claims = store.claim_jobs(
                    "controller", lease["fence_token"], max_jobs=3, lease_ttl_seconds=10
                )
                self.assertEqual(3, len(claims))
                self.assertEqual(
                    ["lane-3", "lane-2", "lane-1"],
                    [item["job"]["job_id"] for item in claims],
                )
                self.assertEqual(
                    {"lane-0"},
                    {item["job_id"] for item in store.list_jobs(statuses=("queued",))},
                )

    def test_job_lease_heartbeat_renews_job_and_attempt_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            with self.open_store(root) as store:
                lease = store.acquire_controller_lease("controller", ttl_seconds=10)
                store.create_job("heartbeat-job", {"kind": "fake"})
                claimed = store.claim_job(
                    "heartbeat-job",
                    "controller",
                    lease["fence_token"],
                    lease_ttl_seconds=1,
                )
                assert claimed
                attempt_id = claimed["attempt"]["attempt_id"]
                before = store.get_job("heartbeat-job")
                before_attempt = store.get_attempt(attempt_id)
                time.sleep(0.1)
                renewed = store.heartbeat_job_lease(
                    "heartbeat-job",
                    attempt_id,
                    "controller",
                    lease["fence_token"],
                    ttl_seconds=2,
                )
                self.assertEqual("running", renewed["job"]["status"])
                self.assertGreater(
                    sqlite_store._parse_time(renewed["job"]["lease_expires_at_utc"]),
                    sqlite_store._parse_time(before["lease_expires_at_utc"]),
                )
                self.assertGreater(
                    sqlite_store._parse_time(renewed["attempt"]["lease_expires_at_utc"]),
                    sqlite_store._parse_time(before_attempt["lease_expires_at_utc"]),
                )
                with self.assertRaises(sqlite_store.FencingError):
                    store.heartbeat_job_lease(
                        "heartbeat-job",
                        attempt_id,
                        "other-controller",
                        lease["fence_token"],
                        ttl_seconds=2,
                    )

    def test_claim_complete_is_atomic_and_idempotent_for_same_fence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            with self.open_store(root) as store:
                lease = store.acquire_controller_lease("controller", ttl_seconds=10)
                store.create_job("job-1", {"kind": "fake"})
                claimed = store.claim_job("job-1", "controller", lease["fence_token"], lease_ttl_seconds=10)
                assert claimed
                attempt_id = claimed["attempt"]["attempt_id"]
                completed = store.complete_job(
                    "job-1",
                    attempt_id,
                    "controller",
                    lease["fence_token"],
                    success=True,
                    result={"text": "done"},
                    artifact_manifest={"sha256": "abc"},
                    validation={"ok": True},
                )
                self.assertEqual("completed", completed["status"])
                self.assertEqual("completed", store.get_attempt(attempt_id)["status"])
                again = store.complete_job(
                    "job-1", attempt_id, "controller", lease["fence_token"], success=True
                )
                self.assertEqual("completed", again["status"])
                self.assertEqual(1, len([e for e in store.list_events("job-1") if e["event_type"] == "job_completed"]))

    def test_retry_backoff_blocks_claim_until_retry_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            with self.open_store(root) as store:
                lease = store.acquire_controller_lease("controller", ttl_seconds=10)
                store.create_job("backoff-job", {"kind": "fake"})
                claim = store.claim_job(
                    "backoff-job", "controller", lease["fence_token"], lease_ttl_seconds=10
                )
                assert claim
                retry = store.complete_job(
                    "backoff-job",
                    claim["attempt"]["attempt_id"],
                    "controller",
                    lease["fence_token"],
                    success=False,
                    error_class="network",
                    retryable=True,
                    retry_delay_seconds=60,
                )
                self.assertEqual("retry", retry["status"])
                self.assertIsNotNone(retry["retry_at_utc"])
                self.assertIsNone(
                    store.claim_next_job("controller", lease["fence_token"], lease_ttl_seconds=10)
                )
                store.connection.execute(
                    "UPDATE jobs SET retry_at_utc = ? WHERE job_id = ?",
                    ("2000-01-01T00:00:00+00:00", "backoff-job"),
                )
                self.assertIsNotNone(
                    store.claim_next_job("controller", lease["fence_token"], lease_ttl_seconds=10)
                )

    def test_fenced_requeue_replaces_failed_packet_and_preserves_attempt_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            with self.open_store(root) as store:
                lease = store.acquire_controller_lease("controller", ttl_seconds=10)
                store.create_job("replan-job", {"packet": "first"})
                claim = store.claim_job(
                    "replan-job", "controller", lease["fence_token"], lease_ttl_seconds=10
                )
                assert claim
                store.complete_job(
                    "replan-job",
                    claim["attempt"]["attempt_id"],
                    "controller",
                    lease["fence_token"],
                    success=False,
                    error_class="quota",
                    error={"class": "quota"},
                )
                requeued = store.requeue_job(
                    "replan-job",
                    "controller",
                    lease["fence_token"],
                    payload={"packet": "replanned"},
                    reason="monitor_cooldown",
                )
                self.assertEqual("queued", requeued["status"])
                self.assertEqual("replanned", requeued["payload"]["packet"])
                self.assertEqual(1, requeued["payload"]["_lad_replan_base_attempt_count"])
                self.assertEqual(1, requeued["attempt_count"])
                self.assertEqual(
                    1,
                    len([e for e in store.list_events("replan-job") if e["event_type"] == "job_requeued"]),
                )
                next_claim = store.claim_job(
                    "replan-job", "controller", lease["fence_token"], lease_ttl_seconds=10
                )
                self.assertIsNotNone(next_claim)
                self.assertEqual(2, next_claim["attempt"]["attempt_no"])

    def test_stale_fence_cannot_complete_after_restart_and_expiry_can_recover(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            path = root / "dispatch.sqlite3"
            old = sqlite_store.SQLiteStore(path)
            try:
                old_lease = old.acquire_controller_lease("old-controller", ttl_seconds=1)
                old.create_job("job-1", {"kind": "fake"})
                claimed = old.claim_job(
                    "job-1", "old-controller", old_lease["fence_token"], lease_ttl_seconds=1
                )
                assert claimed
                attempt_id = claimed["attempt"]["attempt_id"]
                time.sleep(1.15)
                restarted = sqlite_store.SQLiteStore(path)
                try:
                    new_lease = restarted.acquire_controller_lease("new-controller", ttl_seconds=10)
                    self.assertGreater(new_lease["fence_token"], old_lease["fence_token"])
                    self.assertEqual(
                        1,
                        restarted.recover_expired_jobs(
                            "new-controller",
                            new_lease["fence_token"],
                            liveness_by_job={"job-1": "dead"},
                        ),
                    )
                    self.assertEqual("retry", restarted.get_job("job-1")["status"])
                    with self.assertRaises(sqlite_store.FencingError):
                        old.complete_job(
                            "job-1", attempt_id, "old-controller", old_lease["fence_token"], success=True
                        )
                    reclaimed = restarted.claim_next_job(
                        "new-controller", new_lease["fence_token"], lease_ttl_seconds=10
                    )
                    self.assertIsNotNone(reclaimed)
                    self.assertEqual("new-controller", reclaimed["job"]["claimed_by"])
                finally:
                    restarted.close()
            finally:
                old.close()

    def test_process_crash_without_release_is_recoverable_after_expiry(self):
        """A killed worker leaves durable claim state that a new owner can reclaim."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            db_path = root / "dispatch.sqlite3"
            child = """
import os, pathlib, sys
sys.path.insert(0, sys.argv[2])
from sqlite_store import SQLiteStore
store = SQLiteStore(sys.argv[1])
lease = store.acquire_controller_lease('crashed-controller', ttl_seconds=1)
store.create_job('crash-job', {'kind': 'fake'})
assert store.claim_job('crash-job', 'crashed-controller', lease['fence_token'], lease_ttl_seconds=1)
os._exit(0)
"""
            process = subprocess.run(
                [sys.executable, "-c", child, str(db_path), str(ROOT / "scripts")],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, process.returncode, process.stderr)
            time.sleep(1.15)
            with sqlite_store.SQLiteStore(db_path) as restarted:
                lease = restarted.acquire_controller_lease("restarted-controller", ttl_seconds=10)
                self.assertEqual(
                    1,
                    restarted.recover_expired_jobs(
                        "restarted-controller",
                        lease["fence_token"],
                        liveness_by_job={"crash-job": "dead"},
                    ),
                )
                self.assertEqual("retry", restarted.get_job("crash-job")["status"])
                attempts = restarted.list_attempts("crash-job")
                self.assertEqual("abandoned", attempts[0]["status"])
                claim = restarted.claim_next_job(
                    "restarted-controller", lease["fence_token"], lease_ttl_seconds=10
                )
                self.assertIsNotNone(claim)

    def test_strict_recovery_blocks_unknown_or_live_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            with self.open_store(root) as store:
                lease = store.acquire_controller_lease("controller", ttl_seconds=10)
                store.create_job("orphan-job", {"kind": "fake"})
                claim = store.claim_job(
                    "orphan-job", "controller", lease["fence_token"], lease_ttl_seconds=1
                )
                assert claim
                time.sleep(1.15)
                blocked = store.recover_expired_jobs(
                    "controller",
                    lease["fence_token"],
                    liveness_by_job={"orphan-job": "unknown"},
                    strict_liveness=True,
                )
                self.assertEqual(1, blocked)
                self.assertEqual("blocked", store.get_job("orphan-job")["status"])
                self.assertEqual(
                    "recovery_liveness_unknown",
                    store.get_job("orphan-job")["error_class"],
                )
                self.assertEqual(
                    "job_recovery_blocked",
                    store.list_events("orphan-job")[-1]["event_type"],
                )
                self.assertIsNone(
                    store.claim_next_job("controller", lease["fence_token"], lease_ttl_seconds=10)
                )

    def test_event_payloads_are_json_and_event_ids_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            with self.open_store(root) as store:
                event = store.append_event("diagnostic", event_id="event-fixed", payload={"ok": True})
                duplicate = store.append_event("diagnostic", event_id="event-fixed", payload={"ok": False})
                self.assertEqual(event["event_seq"], duplicate["event_seq"])
                self.assertEqual({"ok": True}, store.list_events()[0]["payload"])
                self.assertEqual(1, store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def test_snapshot_is_restart_safe_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            path = root / "dispatch.sqlite3"
            with self.open_store(root) as store:
                lease = store.acquire_controller_lease("controller", ttl_seconds=10)
                store.create_job("job-1", {"nested": [1, 2, 3]})
                snapshot = store.snapshot()
                encoded = json.dumps(snapshot, ensure_ascii=False)
                self.assertIn('"schema_version": 1', encoded)
                self.assertEqual(1, len(snapshot["jobs"]))
                self.assertEqual(1, snapshot["leases"][0]["fence_token"])
                store.release_controller_lease("controller", lease["fence_token"])
            with sqlite_store.SQLiteStore(path) as restarted:
                self.assertEqual({1, 2, 3}, set(restarted.get_job("job-1")["payload"]["nested"]))
                self.assertEqual("released", restarted.get_lease()["status"])


if __name__ == "__main__":
    unittest.main()
