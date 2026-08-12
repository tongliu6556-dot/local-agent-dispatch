#!/usr/bin/env python3
"""Bounded, privacy-preserving importer for legacy JSON dispatch runs.

The historical runtime predates the transactional SQLite controller and has
many incompatible ``state.json`` shapes.  This module deliberately imports
only metadata needed for reconciliation and provenance.  It never copies
prompt text, argv, logs, credentials, or artifacts, and it never mutates the
legacy directory.  Without ``--output-db`` the command is a read-only audit.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys
from typing import Any, Callable, Iterable, Mapping

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

SCHEMA_VERSION = 1
MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_EVENT_LINES = 2000
TERMINAL = {"completed", "failed", "blocked", "review", "artifact_ready_needs_review"}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _load_object(path: pathlib.Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        if path.stat().st_size > MAX_STATE_BYTES:
            return None, "file_too_large"
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, type(exc).__name__
    return (value, None) if isinstance(value, dict) else (None, "not_object")


def _safe_status(value: Any) -> str:
    text = str(value or "unknown").strip().lower()
    return text if len(text) <= 64 else "unknown"


def _safe_string(value: Any, *, limit: int = 160) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or len(text) > limit:
        return None
    return text


def _job_rows(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = state.get("jobs")
    if isinstance(raw, dict):
        raw = list(raw.values())
    if not isinstance(raw, list):
        # Some early runs represented one job directly at the state root.
        raw = [state] if state.get("job_id") or state.get("task_id") else []
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            continue
        job_id = _safe_string(item.get("job_id") or item.get("task_id") or f"legacy-job-{index}")
        if not job_id:
            continue
        attempts = item.get("attempts")
        if not isinstance(attempts, list):
            attempts = []
        exact_model = _safe_string(item.get("model"))
        pool_id = _safe_string(item.get("pool_id"))
        adapter = _safe_string(item.get("adapter"))
        if not exact_model and attempts:
            first = attempts[0] if isinstance(attempts[0], Mapping) else {}
            exact_model = _safe_string(first.get("model"))
            pool_id = pool_id or _safe_string(first.get("pool_id"))
            adapter = adapter or _safe_string(first.get("adapter"))
        rows.append(
            {
                "job_id": job_id,
                "task_id": _safe_string(item.get("task_id")),
                "status": _safe_status(item.get("status") or state.get("status")),
                "model": exact_model,
                "pool_id": pool_id,
                "adapter": adapter,
                "attempt_count": len(attempts),
                "difficulty": item.get("difficulty") if isinstance(item.get("difficulty"), int) else None,
                "legacy_evidence_quality": "legacy_incomplete",
            }
        )
    return rows


def _event_summary(path: pathlib.Path) -> dict[str, Any]:
    if not path.is_file():
        return {"present": False, "lines": 0, "malformed_lines": 0, "event_types": {}}
    event_types: dict[str, int] = {}
    malformed = 0
    lines = 0
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if lines >= MAX_EVENT_LINES:
                    break
                lines += 1
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue
                if isinstance(row, Mapping):
                    event = _safe_string(row.get("event") or row.get("event_type"), limit=80)
                    if event:
                        event_types[event] = event_types.get(event, 0) + 1
    except OSError:
        malformed += 1
    return {
        "present": True,
        "lines": lines,
        "truncated": lines >= MAX_EVENT_LINES,
        "malformed_lines": malformed,
        "event_types": dict(sorted(event_types.items())),
    }


def _pid_liveness(pid: Any) -> str:
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


def summarize_run(
    run_dir: str | os.PathLike[str],
    *,
    reconcile: bool = False,
    liveness_probe: Callable[[Any], str] = _pid_liveness,
) -> dict[str, Any]:
    """Summarize one legacy run without persisting sensitive payloads."""
    root = pathlib.Path(run_dir).expanduser().resolve()
    state_path = root / "state.json"
    state, error = _load_object(state_path)
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_dir": str(root),
        "run_id": root.name,
        "observed_at_utc": utc_now(),
        "evidence_quality": "legacy_incomplete",
        "state_present": state is not None,
        "state_error": error,
        "jobs": [],
        "events": _event_summary(root / "events.jsonl"),
        "liveness": {},
    }
    if state is None:
        return result
    result["run_id"] = _safe_string(state.get("run_id")) or root.name
    result["state_status"] = _safe_status(state.get("status"))
    result["updated_at_utc"] = _safe_string(state.get("updated_at_utc") or state.get("updated_at"))
    jobs = _job_rows(state)
    result["jobs"] = jobs
    if reconcile:
        for job in jobs:
            if job["status"] in {"running", "queued", "retry"}:
                pid = None
                for candidate in (state.get("pid"), state.get("pid_path"), state.get("controller_pid")):
                    if candidate and not isinstance(candidate, Mapping):
                        pid = candidate
                        break
                result["liveness"][job["job_id"]] = liveness_probe(pid)
    result["counts"] = {
        "jobs": len(jobs),
        "running_or_queued": sum(job["status"] in {"running", "queued", "retry"} for job in jobs),
        "terminal": sum(job["status"] in TERMINAL for job in jobs),
    }
    return result


def discover_runs(root: str | os.PathLike[str], *, max_runs: int = 512) -> list[pathlib.Path]:
    base = pathlib.Path(root).expanduser().resolve()
    if not base.is_dir():
        raise NotADirectoryError(str(base))
    paths: list[pathlib.Path] = []
    for candidate in base.rglob("state.json"):
        if len(paths) >= max(1, int(max_runs)):
            break
        if candidate.is_symlink() or not candidate.is_file():
            continue
        paths.append(candidate.parent)
    return sorted(set(paths), key=lambda path: str(path))


def import_to_sqlite(reports: Iterable[Mapping[str, Any]], db_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Write metadata-only legacy rows to a new SQLite database."""
    from sqlite_store import SQLiteStore

    imported = 0
    skipped = 0
    path = pathlib.Path(db_path).expanduser().resolve()
    with SQLiteStore(path) as store:
        owner = "legacy-importer"
        with store.controller_lease(owner, ttl_seconds=120) as lease:
            fence = int(lease["fence_token"])
            for report in reports:
                run_id = _safe_string(report.get("run_id")) or "legacy-run"
                jobs = report.get("jobs")
                if not isinstance(jobs, list):
                    skipped += 1
                    continue
                for job in jobs:
                    if not isinstance(job, Mapping):
                        continue
                    job_id = f"legacy:{run_id}:{_safe_string(job.get('job_id')) or 'unknown'}"
                    payload = {
                        "job_id": job_id,
                        "run_id": run_id,
                        "legacy_source": report.get("run_dir"),
                        "legacy_evidence_quality": "legacy_incomplete",
                        "status_observed": _safe_status(job.get("status")),
                        "model": _safe_string(job.get("model")),
                        "pool_id": _safe_string(job.get("pool_id")),
                        "adapter": _safe_string(job.get("adapter")),
                        "attempt_count_observed": job.get("attempt_count"),
                    }
                    status = _safe_status(job.get("status"))
                    if status not in {"queued", "running", "retry", "completed", "failed", "blocked", "review"}:
                        status = "blocked"
                    try:
                        store.create_job(
                            job_id,
                            payload,
                            run_id=run_id,
                            task_id=_safe_string(job.get("task_id")),
                            status=status,
                            owner_id=owner,
                            fence_token=fence,
                        )
                        imported += 1
                    except Exception:
                        skipped += 1
            store.append_event(
                "legacy_import_completed",
                owner_id=owner,
                fence_token=fence,
                payload={"imported_jobs": imported, "skipped": skipped},
            )
    return {"db_path": str(path), "imported_jobs": imported, "skipped": skipped}


def build_report(root: str | os.PathLike[str], *, reconcile: bool = False, max_runs: int = 512) -> dict[str, Any]:
    runs = [summarize_run(path, reconcile=reconcile) for path in discover_runs(root, max_runs=max_runs)]
    counts: dict[str, int] = {}
    for report in runs:
        for job in report.get("jobs") or []:
            status = str(job.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "report_type": "local-agent-dispatch.legacy-history",
        "read_only": True,
        "provider_execution": False,
        "root": str(pathlib.Path(root).expanduser().resolve()),
        "observed_at_utc": utc_now(),
        "runs": runs,
        "counts": {"runs": len(runs), "jobs_by_status": dict(sorted(counts.items()))},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--output-db")
    parser.add_argument("--reconcile", action="store_true")
    parser.add_argument("--max-runs", type=int, default=512)
    parser.add_argument("--output", default="-")
    args = parser.parse_args(argv)
    try:
        report = build_report(args.root, reconcile=args.reconcile, max_runs=args.max_runs)
        if args.output_db:
            migration = import_to_sqlite(report["runs"], args.output_db)
            report = dict(report)
            report["read_only"] = False
            report["migration"] = migration
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
