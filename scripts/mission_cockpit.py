#!/usr/bin/env python3
"""Build a compact, provenance-aware L0 Mission Cockpit report.

This is a read-only projection over saved mission, controller, monitor and
governor snapshots.  It intentionally omits prompt/argv/log contents.  The
full event stream remains the L3 forensic source; this report is the user's
first screen after returning to a long-running task.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
from typing import Any, Mapping


SCHEMA_VERSION = 1


def _obj(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _status_counts(rows: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for raw in rows:
        row = _obj(raw)
        status = str(row.get("status") or row.get("controller_status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def _mission_goal(mission: Mapping[str, Any]) -> str | None:
    goal = mission.get("goal")
    if isinstance(goal, Mapping):
        value = goal.get("value")
        return str(value) if value else None
    return str(goal) if isinstance(goal, str) and goal.strip() else None


def _claim_ceiling(mission: Mapping[str, Any]) -> dict[str, Any]:
    envelope = _obj(mission.get("claim_envelope"))
    return {
        "allowed": envelope.get("allowed") or [],
        "deferred": envelope.get("deferred") or [],
        "forbidden": envelope.get("forbidden") or [],
        "evidence_level": _obj(envelope.get("evidence_level")).get("value"),
    }


def build_cockpit(
    snapshot: Mapping[str, Any] | None = None,
    *,
    mission: Mapping[str, Any] | None = None,
    governor: Mapping[str, Any] | None = None,
    history: Mapping[str, Any] | None = None,
    now_utc: str | None = None,
) -> dict[str, Any]:
    """Return an L0 report with explicit unknowns and source timestamps."""
    snap = _obj(snapshot)
    mission_obj = _obj(mission)
    gov = _obj(governor)
    jobs = [row for row in snap.get("jobs") or [] if isinstance(row, Mapping)]
    workers = [row for row in snap.get("workers") or [] if isinstance(row, Mapping)]
    if not workers:
        workers = [row for row in jobs if str(_obj(row).get("status")) in {"running", "queued", "retry"}]
    counts = _status_counts(jobs or workers)
    completed = sum(1 for row in jobs if str(_obj(row).get("status")) == "completed")
    total = len(jobs)
    governor_admission = _obj(gov.get("admission"))
    pressure = _obj(gov.get("ram"))

    if mission_obj.get("ambiguous"):
        gate = "mission_compile_review"
    elif any(str(_obj(row).get("status")) in {"failed", "blocked", "review"} for row in jobs):
        gate = "incident_or_replan_review"
    elif any(str(_obj(row).get("status")) in {"queued", "retry"} for row in jobs):
        gate = "plan_and_resource_admission"
    elif any(str(_obj(row).get("status")) == "running" for row in jobs):
        gate = "execution_and_validation"
    elif total and completed == total:
        gate = "claim_or_release_review"
    else:
        gate = "mission_or_plan_review"

    risks: list[dict[str, Any]] = []
    if pressure.get("pressure_tier") in {"conserve", "critical", "emergency"}:
        risks.append({"kind": "local_memory_pressure", "tier": pressure.get("pressure_tier"), "source": "resource_governor"})
    if governor_admission.get("decision") in {"throttle", "pause", "emergency_pause_owned"}:
        risks.append({"kind": "local_admission", "decision": governor_admission.get("decision"), "source": "resource_governor"})
    if not snap:
        risks.append({"kind": "controller_snapshot_missing", "source": "input"})
    if history and history.get("counts", {}).get("stale_running_or_queued"):
        risks.append({"kind": "legacy_state_stale", "count": history["counts"]["stale_running_or_queued"], "source": "legacy_history"})

    active = []
    for row in workers:
        value = _obj(row)
        status = str(value.get("status") or value.get("controller_status") or "unknown")
        if status not in {"running", "queued", "retry", "unknown"}:
            continue
        active.append({
            "job_id": value.get("job_id") or value.get("worker_id"),
            "pool_id": value.get("pool_id"),
            "model": value.get("model"),
            "variant": value.get("variant"),
            "execution_host": value.get("execution_host"),
            "workload_host": value.get("workload_host"),
            "status": status,
        })

    decision = None
    if risks:
        decision = {
            "type": "review_resource_or_state",
            "reason": risks[0].get("kind"),
            "safe_default": "keep_new_local_lanes_blocked",
        }
    elif mission_obj.get("ambiguous"):
        decision = {"type": "resolve_mission_ambiguity", "safe_default": "do_not_dispatch"}
    elif gate == "claim_or_release_review":
        decision = {"type": "claim_or_release_review", "safe_default": "keep_claim_ceiling"}

    return {
        "schema_version": SCHEMA_VERSION,
        "report_type": "local-agent-dispatch.mission-cockpit",
        "read_only": True,
        "provider_execution": False,
        "observed_at_utc": now_utc or dt.datetime.now(dt.timezone.utc).isoformat(),
        "mission": {
            "mission_id": mission_obj.get("mission_id"),
            "goal": _mission_goal(mission_obj),
            "claim_ceiling": _claim_ceiling(mission_obj),
            "ambiguous": mission_obj.get("ambiguous") or [],
        },
        "current_gate": gate,
        "delta": {
            "job_status_counts": counts,
            "validated_completed": completed,
            "total_jobs": total,
            "history_source": "controller_snapshot" if snap else "unknown",
        },
        "verified_progress": {
            "completed_jobs": completed,
            "total_jobs": total,
            "completion_fraction": (completed / total) if total else None,
            "evidence": "durable_controller_terminal_state" if total else "unknown",
        },
        "risks": risks,
        "active_assignments": active,
        "decision_required": decision,
        "resource_summary": {
            "pressure_tier": pressure.get("pressure_tier"),
            "available_bytes": pressure.get("available_bytes"),
            "max_new_local_lanes": governor_admission.get("max_new_local_lanes"),
            "source": gov.get("observed_at_utc"),
        },
        "sources": {
            "controller_snapshot": bool(snap),
            "mission_spec": bool(mission_obj),
            "resource_governor": bool(gov),
            "legacy_history": bool(history),
            "raw_prompt_persisted": False,
            "raw_argv_persisted": False,
        },
    }


def _load(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    value = json.loads(pathlib.Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--mission")
    parser.add_argument("--governor")
    parser.add_argument("--history")
    parser.add_argument("--output", default="-")
    args = parser.parse_args(argv)
    try:
        report = build_cockpit(
            _load(args.snapshot),
            mission=_load(args.mission),
            governor=_load(args.governor),
            history=_load(args.history),
        )
        text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output == "-":
            print(text, end="")
        else:
            target = pathlib.Path(args.output).expanduser().resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.tmp")
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(target)
        return 0
    except Exception as exc:
        print(json.dumps({"schema_version": SCHEMA_VERSION, "ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
