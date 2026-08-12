#!/usr/bin/env python3
"""Transactional SQLite controller for explicit, prepared task packets.

This is the first SQLite-backed execution path.  It reuses the existing
provider adapter/build/validation helpers, while queue claim, lease fencing,
attempt transitions, and event records are committed by ``SQLiteStore`` in
short WAL transactions.  It is intentionally opt-in; the legacy JSON
controller remains available during migration.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import pathlib
import sys
import time
import uuid
from typing import Any

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import continuity_controller as continuity  # noqa: E402
from sqlite_store import (  # noqa: E402
    FencingError,
    JobConflict,
    ReservationAdmissionError,
    SQLiteStore,
)
import resource_governor as governor  # noqa: E402


def load_json(path: pathlib.Path) -> Any:
    return json.loads(path.expanduser().read_text(encoding="utf-8"))


def write_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def priority_value(job: dict[str, Any]) -> int:
    value = job.get("priority", 0)
    if isinstance(value, int):
        return int(value)
    return {"low": 1, "normal": 2, "high": 3, "critical": 4}.get(str(value).lower(), 2)


def _sha_fresh(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> bool | None:
    if not after:
        return None
    by_path = {str(row.get("path")): row for row in before}
    if any(row.get("error") for row in before):
        return False
    for row in after:
        if not row.get("exists") or not row.get("sha256"):
            return False
        old = by_path.get(str(row.get("path")), {})
        if old.get("exists"):
            if old.get("sha256") and old.get("sha256") == row.get("sha256"):
                return False
            if not old.get("sha256") and old.get("size") == row.get("size") and old.get("mtime") == row.get("mtime"):
                return False
    return True


class SQLiteController:
    def __init__(
        self,
        db_path: pathlib.Path,
        *,
        workspace: pathlib.Path | None = None,
        inventory: pathlib.Path | None = None,
        runtime_state: pathlib.Path | None = None,
        lease_ttl_seconds: int = 90,
        heartbeat_interval_seconds: float | None = None,
        enforce_reservations: bool = False,
    ) -> None:
        self.db_path = db_path.expanduser().resolve()
        self.workspace = (workspace or pathlib.Path.cwd()).expanduser().resolve()
        self.inventory_path = inventory.expanduser().resolve() if inventory else None
        self.runtime_state = runtime_state.expanduser().resolve() if runtime_state else None
        self.lease_ttl_seconds = max(30, int(lease_ttl_seconds))
        if heartbeat_interval_seconds is not None and float(heartbeat_interval_seconds) <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")
        self.heartbeat_interval_seconds = (
            float(heartbeat_interval_seconds) if heartbeat_interval_seconds is not None else None
        )
        self.enforce_reservations = bool(enforce_reservations)

    def _state(self) -> dict[str, Any]:
        state: dict[str, Any] = {
            "workspace": str(self.workspace),
        }
        if self.inventory_path:
            state["inventory"] = str(self.inventory_path)
        if self.runtime_state:
            state["runtime_state"] = str(self.runtime_state)
        return state

    def _hosts(self) -> dict[str, dict[str, Any]]:
        if not self.inventory_path or not self.inventory_path.exists():
            return {}
        try:
            return continuity.load_inventory({"inventory": str(self.inventory_path)})
        except (OSError, ValueError, KeyError):
            return {}

    @staticmethod
    def _pid_liveness(pid: Any) -> str:
        """Return conservative local process evidence for stale recovery."""

        if pid in (None, ""):
            return "unknown"
        try:
            value = int(pid)
        except (TypeError, ValueError):
            return "unknown"
        if value <= 1:
            return "unknown"
        try:
            os.kill(value, 0)
        except ProcessLookupError:
            return "dead"
        except (PermissionError, OSError):
            return "unknown"
        return "alive"

    def _recovery_liveness(self, store: SQLiteStore) -> dict[str, str]:
        """Collect local PID evidence before SQLite stale-row recovery.

        SSH/remote attempts and packets without an explicit PID breadcrumb are
        intentionally ``unknown``.  The durable store then blocks them rather
        than risking duplicate writes; a human or remote worker must provide a
        stronger handoff before retrying.
        """

        evidence: dict[str, str] = {}
        for record in store.expired_running_jobs():
            job = dict(record.get("job") or {})
            job_id = str(job.get("job_id") or "")
            attempt_row = dict(record.get("attempt") or {})
            if not job_id or not attempt_row:
                if job_id:
                    evidence[job_id] = "unknown"
                continue
            try:
                attempt = self._attempt_spec(job, attempt_row)
            except (TypeError, ValueError, KeyError):
                evidence[job_id] = "unknown"
                continue
            if str(attempt.get("transport") or "local") != "local":
                evidence[job_id] = "unknown"
                continue
            pid = attempt.get("pid") or job.get("pid")
            pid_path = attempt.get("pid_path") or job.get("pid_path")
            if pid_path:
                try:
                    workspace = pathlib.Path(
                        str(job.get("workspace") or self.workspace)
                    ).expanduser().resolve()
                    path = continuity.resolve_path(str(pid_path), workspace)
                    pid = path.read_text(encoding="utf-8").strip()
                except (OSError, ValueError):
                    evidence[job_id] = "unknown"
                    continue
            evidence[job_id] = self._pid_liveness(pid)
        return evidence

    @staticmethod
    def _resource_request(packet: dict[str, Any]) -> dict[str, Any]:
        request = packet.get("resource_request")
        if isinstance(request, dict) and request:
            return dict(request)
        estimate = packet.get("resource_estimate")
        if not isinstance(estimate, dict):
            return {}
        # Planner estimates use the same names as resource requests for the
        # core dimensions.  Keep this compatibility mapping deliberately
        # small; unknown values must not be invented at claim time.
        mapped: dict[str, Any] = {}
        for key in (
            "cpu_cores", "ram_gib", "gpu_count", "vram_gib",
            "new_disk_gib", "compute_minutes", "network_gib",
        ):
            if key in estimate:
                mapped[key] = estimate[key]
        if "vram_gib" in mapped and "vram_gib_per_gpu" not in mapped:
            mapped["vram_gib_per_gpu"] = mapped.pop("vram_gib")
        return mapped

    @staticmethod
    def _first_attempt(packet: dict[str, Any]) -> dict[str, Any]:
        attempts = packet.get("attempts")
        return dict(attempts[0]) if isinstance(attempts, list) and attempts and isinstance(attempts[0], dict) else {}

    def _ensure_reservations(
        self,
        store: SQLiteStore,
        owner_id: str,
        fence_token: int,
        *,
        max_lanes: int,
    ) -> list[dict[str, Any]]:
        """Reserve the next resource-bearing jobs before atomic claim.

        Legacy packets without a resource request remain compatible.  New
        planner packets are strict: local work needs a live governor admission;
        remote work needs a verified host-capacity evidence flag in the packet.
        No process is paused or killed here.
        """
        if not self.enforce_reservations:
            return []
        active = {
            str(row.get("job_id")): row
            for row in store.list_reservations(statuses=("active",))
        }
        diagnostics: list[dict[str, Any]] = []
        candidates = store.list_jobs(statuses=("queued", "retry"))[: max(1, int(max_lanes) * 8)]
        local_observation: tuple[dict[str, Any], list[dict[str, Any]]] | None = None
        for row in candidates:
            job_id = str(row.get("job_id") or "")
            packet = dict(row.get("payload") or {})
            request = self._resource_request(packet)
            required = bool(packet.get("resource_reservation_required") or request)
            if not required or not job_id or job_id in active:
                continue
            attempt = self._first_attempt(packet)
            transport = str(attempt.get("transport") or packet.get("execution_transport") or "local")
            admission: dict[str, Any]
            if transport == "ssh":
                verified = bool(
                    packet.get("remote_resource_evidence_verified")
                    or request.get("remote_capacity_verified")
                )
                admission = {
                    "allowed": verified,
                    "decision": "admit" if verified else "block",
                    "reason": "remote_resource_evidence_verified" if verified else "remote_resource_evidence_missing",
                    "source": "packet",
                }
            else:
                ram_value = request.get("ram_gib")
                try:
                    ram_gib = float(ram_value)
                except (TypeError, ValueError):
                    ram_gib = 0.0
                if ram_gib <= 0:
                    admission = {
                        "allowed": False,
                        "decision": "block",
                        "reason": "unknown_local_ram_request",
                        "source": "resource_request",
                    }
                else:
                    if local_observation is None:
                        local_observation = governor.observe_local()
                    ram, processes = local_observation
                    report = governor.build_report(
                        ram=ram,
                        processes=processes,
                        requested_lanes=1,
                        per_lane_peak_bytes=max(256 * governor.MIB, int(ram_gib * governor.GIB)),
                        max_local_lanes=1,
                    )
                    admission = {
                        "allowed": bool(report["admission"].get("local_agent_launch_allowed")),
                        "decision": report["admission"].get("decision"),
                        "reason": report["ram"].get("pressure_tier"),
                        "source": "resource_governor",
                        "observed_at_utc": report.get("observed_at_utc"),
                        "pressure_tier": report["ram"].get("pressure_tier"),
                        "max_new_local_lanes": report["admission"].get("max_new_local_lanes"),
                    }
            try:
                reservation = store.reserve_resources(
                    job_id,
                    owner_id,
                    fence_token,
                    request,
                    admission=admission,
                    ttl_seconds=self.lease_ttl_seconds,
                )
                diagnostics.append({"job_id": job_id, "status": "reserved", "reservation_id": reservation.get("reservation_id"), "admission": admission})
            except ReservationAdmissionError:
                diagnostics.append({"job_id": job_id, "status": "blocked", "admission": admission})
        return diagnostics

    def enqueue(self, packet: dict[str, Any]) -> dict[str, Any]:
        # Keep both controller backends behind the same ingress contract.
        # SQLite is opt-in, but it must not become an unvalidated escape hatch.
        packet = dict(packet)
        packet.setdefault("schema_version", 1)
        continuity.validate_task_packet(packet)
        job_id = str(packet.get("job_id") or "")
        packet["packet_validation"] = {"mode": "strict", "backend": "sqlite"}
        with SQLiteStore(self.db_path) as store:
            owner_id = f"sqlite-enqueue-{uuid.uuid4().hex[:12]}"
            with store.controller_lease(owner_id, ttl_seconds=self.lease_ttl_seconds) as lease:
                fence_token = int(lease["fence_token"])
                existing = store.get_job(job_id)
                if existing is not None:
                    status = str(existing.get("status") or "")
                    if status in {"failed", "blocked", "pending"}:
                        return store.requeue_job(
                            job_id,
                            owner_id,
                            fence_token,
                            payload=packet,
                            priority=priority_value(packet),
                            reason="approved_replan",
                        )
                try:
                    return store.create_job(
                        job_id,
                        packet,
                        priority=priority_value(packet),
                        owner_id=owner_id,
                        fence_token=fence_token,
                    )
                except JobConflict:
                    # Preserve the original conflict contract for queued,
                    # running, retry, or completed rows.  Only the explicit
                    # terminal-state transition above may replace a packet.
                    raise

    def _attempt_spec(self, job: dict[str, Any], attempt_row: dict[str, Any]) -> dict[str, Any]:
        attempts = job.get("attempts") or []
        attempt_no = max(1, int(attempt_row.get("attempt_no") or 1))
        replan_base = max(0, int(job.get("_lad_replan_base_attempt_count") or 0))
        packet_index = attempt_no - replan_base - 1
        if packet_index < 0:
            packet_index = 0
        if packet_index >= len(attempts):
            raise ValueError(f"job {job.get('job_id')} has no packet attempt {packet_index + 1}")
        attempt = dict(attempts[packet_index])
        attempt["attempt_id"] = str(attempt_row.get("attempt_id") or attempt.get("attempt_id") or "")
        if not attempt["attempt_id"]:
            raise ValueError("claimed attempt has no attempt_id")
        return attempt

    def _execute_claim(
        self,
        store: SQLiteStore,
        claim: dict[str, Any],
        owner_id: str,
        fence_token: int,
    ) -> dict[str, Any]:
        row = claim.get("job") or {}
        job = dict(row.get("payload") or {})
        job["job_id"] = str(row.get("job_id") or job.get("job_id") or "")
        attempt_row = dict(claim.get("attempt") or {})
        attempt = self._attempt_spec(job, attempt_row)
        state = self._state()
        hosts = self._hosts()
        workspace = pathlib.Path(str(job.get("workspace") or self.workspace)).expanduser().resolve()
        artifact_host = None
        if str(attempt.get("transport") or "local") == "ssh":
            artifact_host = hosts.get(str(attempt.get("host_id") or ""))
        before = (
            continuity.remote_artifact_facts(job, attempt, artifact_host)
            if artifact_host
            else continuity.artifact_facts(job, workspace)
        )
        result_source_before = None
        result_source = attempt.get("result_source_path") or job.get("result_source_path")
        if result_source and not artifact_host:
            result_source_before = continuity.file_fact(
                continuity.resolve_path(str(result_source), workspace)
            )

        output = ""
        returncode = 2
        timed_out = False
        output_path: str | None = None
        validation: dict[str, Any] | None = None
        try:
            argv, cwd, output_path, stdin_payload = continuity.build_attempt(job, attempt, state, hosts)
            timeout_seconds = max(30, int(attempt.get("timeout_seconds", job.get("timeout_seconds", 3600))))
            pid_path = None
            if attempt.get("pid_path") or job.get("pid_path"):
                pid_value = attempt.get("pid_path") or job.get("pid_path")
                pid_path = str(continuity.resolve_path(str(pid_value), workspace))
            result = continuity._load_process_group_run().run_in_process_group(
                argv,
                cwd=str(cwd) if cwd else None,
                stdin_data=stdin_payload,
                timeout_seconds=timeout_seconds,
                pid_path=pid_path,
            )
            output = result.stdout or ""
            returncode = int(result.returncode)
            timed_out = bool(result.timed_out)
            if timed_out:
                returncode = 124
                output += "\ncontinuity controller: attempt timed out\n"
                output_path = None
            if output_path and returncode == 0:
                continuity.publish_attempt_output(
                    output,
                    output_path,
                    str(result_source) if result_source else None,
                    workspace,
                    result_source_before,
                )
            if returncode == 0:
                validation = continuity.run_validation(job, attempt, state, hosts)
                if validation is None and job.get("validation_required") is True:
                    validation = {
                        "ok": False,
                        "returncode": 2,
                        "timed_out": False,
                        "error": "validation is required but no validator was configured",
                    }
                if validation is not None and not validation.get("ok"):
                    returncode = int(validation.get("returncode") or 2)
        except Exception as exc:
            output += f"\nsqlite controller: {type(exc).__name__}: {exc}\n"
            returncode = 2

        facts = (
            continuity.remote_artifact_facts(job, attempt, artifact_host)
            if artifact_host
            else continuity.artifact_facts(job, workspace)
        )
        freshness = _sha_fresh(before, facts)
        if facts and job.get("accept_existing_artifacts"):
            freshness = bool(validation and validation.get("ok"))
        artifacts_ok = (
            all(row.get("exists") and row.get("size", 0) > 0 and row.get("sha256") for row in facts)
            and freshness
            if facts
            else returncode == 0
        )
        success = bool(returncode == 0 and artifacts_ok)
        error_class = None if success else continuity.classify(output, timed_out)
        try:
            continuity.record_runtime_feedback(
                state, job, attempt, success=success, error_class=error_class, output=output
            )
        except (OSError, ValueError):
            # Runtime feedback is supplementary; SQLite completion evidence is
            # still authoritative and must not be lost because a sidecar is
            # unavailable.
            pass
        attempt_count = int(attempt_row.get("attempt_no") or 1)
        fallback = set(attempt.get("fallback_on") or ["quota", "auth", "network", "capability"])
        retryable = (not success) and error_class in fallback and attempt_count < len(job.get("attempts") or [])
        retry_delay_seconds = None
        if retryable:
            # Keep transient quota/network failures from hot-looping.  A
            # packet may choose a bounded base delay for its own policy; the
            # controller applies exponential growth per attempt and caps it
            # so a durable worker remains observable rather than sleeping
            # indefinitely.  Replan is a separate explicit queue transition
            # and is intentionally not delayed here.
            try:
                base_delay = float(attempt.get("retry_backoff_seconds", 5.0))
            except (TypeError, ValueError):
                base_delay = 5.0
            base_delay = min(300.0, max(1.0, base_delay))
            retry_delay_seconds = min(
                900.0, base_delay * (2 ** max(0, attempt_count - 1))
            )
        completed = store.complete_job(
            job["job_id"],
            str(attempt_row["attempt_id"]),
            owner_id,
            fence_token,
            success=success,
            result={"output": output[-8000:], "returncode": returncode, "timed_out": timed_out},
            artifact_manifest=facts,
            validation=validation,
            error_class=error_class,
            error={"class": error_class, "output": output[-2000:]} if not success else None,
            retryable=retryable,
            retry_delay_seconds=retry_delay_seconds,
        )
        return {
            "job_id": job["job_id"],
            "attempt_id": attempt_row["attempt_id"],
            "status": completed.get("status"),
            "success": success,
            "retryable": retryable,
            "retry_delay_seconds": retry_delay_seconds,
            "error_class": error_class,
            "artifact_freshness_verified": freshness,
            "validation": validation,
        }

    def run(
        self,
        *,
        once: bool = False,
        max_idle_rounds: int = 0,
        poll_seconds: float = 1.0,
        max_lanes: int = 1,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(max_lanes, bool) or int(max_lanes) < 1:
            raise ValueError("max_lanes must be a positive integer")
        if isinstance(max_idle_rounds, bool) or int(max_idle_rounds) < 0:
            raise ValueError("max_idle_rounds must be zero (infinite) or a positive integer")
        lanes = min(int(max_lanes), 32)
        owner = owner_id or f"sqlite-controller-{uuid.uuid4().hex[:12]}"
        results: list[dict[str, Any]] = []
        reservation_diagnostics: list[dict[str, Any]] = []
        idle = 0
        with SQLiteStore(self.db_path) as store:
            # Make the fencing heartbeat explicit at the controller boundary.
            # SQLiteStore also has a safe default, but keeping the interval
            # here prevents a future backend/default change from allowing a
            # long provider call to outlive the controller lease.
            heartbeat_interval = self.heartbeat_interval_seconds
            if heartbeat_interval is None:
                heartbeat_interval = max(1.0, min(float(self.lease_ttl_seconds) / 3.0, 30.0))
            with store.controller_lease(
                owner,
                ttl_seconds=self.lease_ttl_seconds,
                heartbeat_interval_seconds=heartbeat_interval,
            ) as lease:
                fence = int(lease["fence_token"])
                recovery_liveness = self._recovery_liveness(store)
                store.recover_expired_jobs(
                    owner,
                    fence,
                    liveness_by_job=recovery_liveness,
                    strict_liveness=True,
                )

                def execute_claim_with_heartbeats(claim: dict[str, Any]) -> dict[str, Any]:
                    claim_job = claim.get("job") or {}
                    claim_attempt = claim.get("attempt") or {}
                    job_id = str(claim_job.get("job_id") or "")
                    attempt_id = str(claim_attempt.get("attempt_id") or "")
                    if not job_id or not attempt_id:
                        raise ValueError("claim is missing job_id or attempt_id")
                    # A controller lease protects the scheduler; this
                    # per-attempt heartbeat protects the actual work row
                    # while the provider runs outside SQLite transactions.
                    with store.job_lease_heartbeat(
                        job_id,
                        attempt_id,
                        owner,
                        fence,
                        ttl_seconds=self.lease_ttl_seconds,
                        heartbeat_interval_seconds=heartbeat_interval,
                    ):
                        return self._execute_claim(store, claim, owner, fence)

                def execute_claim_safely(claim: dict[str, Any]) -> dict[str, Any]:
                    """Contain one lane failure without cancelling sibling lanes.

                    Provider/adapter exceptions normally get converted by
                    ``_execute_claim`` into a fenced terminal result.  A
                    failure in the surrounding heartbeat, packet decoding, or
                    an overridden adapter can still escape that boundary.  A
                    thread-pool ``future.result()`` must not let such an
                    exception tear down the whole batch and strand the other
                    claims in ``running``.  We therefore make a best-effort
                    terminal controller failure while preserving a structured
                    result if the lease has already been fenced.
                    """
                    try:
                        return execute_claim_with_heartbeats(claim)
                    except Exception as exc:  # pragma: no cover - exercised by fake-lane test
                        claim_job = claim.get("job") or {}
                        claim_attempt = claim.get("attempt") or {}
                        job_id = str(claim_job.get("job_id") or "")
                        attempt_id = str(claim_attempt.get("attempt_id") or "")
                        # Persist only the exception class.  Adapter/provider
                        # output can contain prompt or credential material;
                        # detailed diagnostics stay in the provider boundary.
                        safe_output = f"sqlite controller lane exception: {type(exc).__name__}"
                        result: dict[str, Any] = {
                            "job_id": job_id,
                            "attempt_id": attempt_id,
                            "status": "failed",
                            "success": False,
                            "retryable": False,
                            "error_class": "controller",
                            "artifact_freshness_verified": False,
                            "validation": None,
                        }
                        if not job_id or not attempt_id:
                            result["completion_error"] = "claim is missing job_id or attempt_id"
                            return result
                        try:
                            completed = store.complete_job(
                                job_id,
                                attempt_id,
                                owner,
                                fence,
                                success=False,
                                result={"output": safe_output, "returncode": 2, "timed_out": False},
                                artifact_manifest=[],
                                validation=None,
                                error_class="controller",
                                error={"class": "controller", "output": safe_output},
                                retryable=False,
                            )
                            result["status"] = completed.get("status", "failed")
                        except Exception as completion_exc:
                            # A lost/fenced lease cannot safely mutate the
                            # row.  Leave it for durable recovery and expose
                            # that fact instead of hiding the sibling result.
                            result["completion_error"] = type(completion_exc).__name__
                        return result

                while True:
                    reservation_diagnostics.extend(
                        self._ensure_reservations(
                            store,
                            owner,
                            fence,
                            max_lanes=lanes,
                        )
                    )
                    claims = store.claim_jobs(
                        owner,
                        fence,
                        max_jobs=lanes,
                        lease_ttl_seconds=self.lease_ttl_seconds,
                        require_reservation=self.enforce_reservations,
                    )
                    if not claims:
                        idle += 1
                        # ``0`` is the durable-worker mode: stay alive while
                        # the queue is empty so a later enqueue can continue
                        # after the originating chat/session disappears.
                        if once or (max_idle_rounds and idle >= int(max_idle_rounds)):
                            break
                        time.sleep(max(0.1, poll_seconds))
                        continue
                    idle = 0
                    if len(claims) == 1:
                        results.append(execute_claim_safely(claims[0]))
                    else:
                        # Claiming is atomic; provider work is outside the
                        # transaction and can use independent write scopes.
                        # SQLiteStore serializes the short completion/event
                        # transactions safely.
                        with concurrent.futures.ThreadPoolExecutor(max_workers=len(claims)) as pool:
                            futures = [pool.submit(execute_claim_safely, claim) for claim in claims]
                            results.extend(future.result() for future in futures)
                    if once:
                        break
            snapshot = store.snapshot()
        return {
            "schema_version": 1,
            "ok": True,
            "backend": "sqlite",
            "results": results,
            "reservation_diagnostics": reservation_diagnostics,
            "snapshot": snapshot,
        }

    def status(self) -> dict[str, Any]:
        with SQLiteStore(self.db_path) as store:
            return {"schema_version": 1, "ok": True, "backend": "sqlite", "snapshot": store.snapshot()}

    def resume(self, *, owner_id: str | None = None) -> dict[str, Any]:
        owner = owner_id or f"sqlite-resume-{uuid.uuid4().hex[:12]}"
        with SQLiteStore(self.db_path) as store:
            with store.controller_lease(owner, ttl_seconds=self.lease_ttl_seconds) as lease:
                recovery_liveness = self._recovery_liveness(store)
                recovered = store.recover_expired_jobs(
                    owner,
                    int(lease["fence_token"]),
                    liveness_by_job=recovery_liveness,
                    strict_liveness=True,
                )
            return {"schema_version": 1, "ok": True, "backend": "sqlite", "recovered": recovered, "snapshot": store.snapshot()}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)

    enqueue = sub.add_parser("enqueue")
    enqueue.add_argument("--db", required=True)
    enqueue.add_argument("--job-file", required=True)

    run = sub.add_parser("run")
    run.add_argument("--db", required=True)
    run.add_argument("--workspace", default=".")
    run.add_argument("--inventory")
    run.add_argument("--runtime-state")
    run.add_argument("--once", action="store_true")
    run.add_argument(
        "--max-idle-rounds", type=int, default=0,
        help="empty-queue polls before exit; 0 keeps the durable worker alive",
    )
    run.add_argument("--poll-seconds", type=float, default=1.0)
    run.add_argument("--max-lanes", type=int, default=1)
    run.add_argument("--owner-id")
    run.add_argument(
        "--enforce-reservations", action=argparse.BooleanOptionalAction, default=True,
        help="require planner resource packets to hold a live admission reservation",
    )

    status = sub.add_parser("status")
    status.add_argument("--db", required=True)

    resume = sub.add_parser("resume")
    resume.add_argument("--db", required=True)
    resume.add_argument("--owner-id")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "enqueue":
            packet = load_json(pathlib.Path(args.job_file))
            if not isinstance(packet, dict):
                raise ValueError("job file must contain an object")
            result = SQLiteController(pathlib.Path(args.db)).enqueue(packet)
            write_json({"schema_version": 1, "ok": True, "backend": "sqlite", "job": result})
            return 0
        controller = SQLiteController(
            pathlib.Path(args.db),
            workspace=pathlib.Path(getattr(args, "workspace", ".")),
            inventory=pathlib.Path(args.inventory) if getattr(args, "inventory", None) else None,
            runtime_state=pathlib.Path(args.runtime_state) if getattr(args, "runtime_state", None) else None,
        )
        if args.command == "run":
            controller.enforce_reservations = bool(args.enforce_reservations)
            write_json(controller.run(
                once=args.once,
                max_idle_rounds=args.max_idle_rounds,
                poll_seconds=args.poll_seconds,
                max_lanes=args.max_lanes,
                owner_id=args.owner_id,
            ))
        elif args.command == "status":
            write_json(controller.status())
        elif args.command == "resume":
            write_json(controller.resume(owner_id=args.owner_id))
        return 0
    except Exception as exc:
        write_json({"schema_version": 1, "ok": False, "backend": "sqlite", "error": f"{type(exc).__name__}: {exc}"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
