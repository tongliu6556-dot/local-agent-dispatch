#!/usr/bin/env python3
"""Convert a planner assignment into a safe, durable controller packet.

The planner deliberately knows *where* work should run, not how a provider
CLI should be invoked.  This bridge is the explicit boundary: it copies only
placement/model fields from a plan, requires an adapter contract, validates
paths and artifacts, and defaults to a dry-run report.  It never starts a
provider or downloads data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shlex
from pathlib import PurePosixPath
from typing import Any

try:
    from continuity_controller import resolve_path
except ImportError:  # pragma: no cover - package/direct script fallback
    from .continuity_controller import resolve_path  # type: ignore


SCHEMA_VERSION = 1
DESKTOP_ADAPTERS = {"codex", "cursor", "antigravity", "opencode"}


class BridgeError(ValueError):
    """Raised when a plan assignment cannot be made executable safely."""


def _canonical_digest(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _jobs_by_id(jobs: Any) -> dict[str, dict[str, Any]]:
    rows = jobs.get("jobs", []) if isinstance(jobs, dict) else jobs
    if not isinstance(rows, list):
        raise BridgeError("jobs must be a list or an object containing jobs")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("job_id"):
            raise BridgeError("every job must be an object with job_id")
        job_id = str(row["job_id"])
        if job_id in result:
            raise BridgeError(f"duplicate job_id: {job_id}")
        result[job_id] = row
    return result


def _host_rows(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    hosts = state.get("hosts") or state.get("compute_hosts") or {}
    if isinstance(hosts, list):
        return {str(row.get("host_id")): row for row in hosts if isinstance(row, dict) and row.get("host_id")}
    if isinstance(hosts, dict):
        return {str(key): dict(value or {}, host_id=key) for key, value in hosts.items()}
    return {}


def _require_path(value: Any, workspace: pathlib.Path, field: str) -> str:
    if not value:
        raise BridgeError(f"missing {field}")
    try:
        return str(resolve_path(str(value), workspace))
    except ValueError as exc:
        raise BridgeError(f"{field}: {exc}") from exc


def _require_remote_path(value: Any, remote_root: str, field: str) -> str:
    """Resolve a remote POSIX path below the declared host project root."""
    if not isinstance(value, str) or not value.strip():
        raise BridgeError(f"missing {field}")
    root = PurePosixPath(remote_root)
    raw = PurePosixPath(value)
    if not root.is_absolute():
        raise BridgeError("remote_workspace must be an absolute POSIX path")
    if any(part in {"", ".", ".."} for part in raw.parts if part != "/"):
        raise BridgeError(f"{field}: unsafe remote path component")
    candidate = raw if raw.is_absolute() else root / raw
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise BridgeError(f"{field}: path escapes remote workspace") from exc
    return str(candidate)


def _artifact_list(job: dict[str, Any], assignment: dict[str, Any]) -> list[Any]:
    values = job.get("required_artifacts")
    if values is None:
        values = job.get("required_artifact")
    if values is None:
        values = assignment.get("required_artifact")
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list) or not values or not all(values):
        raise BridgeError("required_artifacts must be a non-empty list")
    return list(values)


def _coerce_validation(job: dict[str, Any], spec: dict[str, Any]) -> list[str]:
    raw = job.get("validation_argv")
    if raw is None:
        raw = job.get("validation_command")
    if raw is None:
        raw = spec.get("validation_argv") or spec.get("validation_command")
    if isinstance(raw, str):
        argv = shlex.split(raw)
    elif isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        argv = list(raw)
    else:
        raise BridgeError("validation command is required and must be argv or a shell-free string")
    if not argv:
        raise BridgeError("validation command must not be empty")
    executable = pathlib.Path(argv[0]).name.lower()
    if executable in {"sh", "bash", "zsh", "fish", "cmd", "powershell", "pwsh"} and any(
        item in {"-c", "/c", "-command"} for item in argv[1:]
    ):
        raise BridgeError("shell validation is not allowed; use an explicit argv executable")
    return argv


def _expand_argv(raw: Any, assignment: dict[str, Any], workspace: pathlib.Path) -> list[str]:
    if not isinstance(raw, list) or not raw or not all(isinstance(item, str) for item in raw):
        raise BridgeError("command adapter requires an explicit non-empty argv list")
    values = {
        "model": str(assignment.get("model") or ""),
        "variant": str(assignment.get("variant") or ""),
        "workspace": str(workspace),
    }
    result: list[str] = []
    for item in raw:
        if "{prompt}" in item or "{task}" in item:
            raise BridgeError("argv templates may not interpolate prompt/task text")
        try:
            result.append(item.format_map(values))
        except (KeyError, ValueError) as exc:
            raise BridgeError(f"invalid argv template: {item}") from exc
    return result


def assignment_to_packet(
    assignment: dict[str, Any],
    job: dict[str, Any],
    state: dict[str, Any],
    adapter_registry: dict[str, Any],
    *,
    plan_digest: str,
) -> dict[str, Any]:
    """Build one deterministic packet or raise :class:`BridgeError`."""
    job_id = str(assignment.get("job_id") or job.get("job_id") or "")
    if not job_id:
        raise BridgeError("assignment is missing job_id")
    model = str(assignment.get("model") or "")
    if not model:
        raise BridgeError(f"{job_id}: planner assignment must contain exact model")
    pool_id = str(assignment.get("pool_id") or "")
    if not pool_id:
        raise BridgeError(f"{job_id}: planner assignment must contain pool_id")
    spec = adapter_registry.get(pool_id) or adapter_registry.get(pool_id.split(".", 1)[0])
    if not isinstance(spec, dict):
        raise BridgeError(f"{job_id}: missing adapter contract for {pool_id}")
    adapter = str(spec.get("adapter") or "")
    if not adapter:
        raise BridgeError(f"{job_id}: adapter contract has no adapter")
    provider = str(spec.get("provider") or pool_id.split(".", 1)[0])

    execution_host = str(assignment.get("execution_host") or "")
    workload_host = str(assignment.get("workload_host") or execution_host)
    execution_transport = str(assignment.get("execution_transport") or spec.get("transport") or "local")
    workload_transport = str(assignment.get("workload_transport") or execution_transport)
    hosts = _host_rows(state)
    if execution_host and execution_host not in hosts:
        raise BridgeError(f"{job_id}: unknown execution_host {execution_host}")
    if workload_host and workload_host not in hosts:
        raise BridgeError(f"{job_id}: unknown workload_host {workload_host}")

    if adapter in DESKTOP_ADAPTERS or provider in DESKTOP_ADAPTERS:
        if execution_transport != "local":
            raise BridgeError(f"{job_id}: desktop adapter cannot execute over SSH")
        if workload_host != execution_host:
            if not (spec.get("supports_split_placement") and job.get("workload_wrapper")):
                raise BridgeError(f"{job_id}: split_placement_requires_remote_wrapper")
    if adapter == "server_local":
        if workload_host != execution_host or workload_transport != "ssh":
            raise BridgeError(f"{job_id}: server_local requires one SSH execution/workload host")
    if adapter == "server_openai":
        base_url = str(spec.get("base_url") or job.get("base_url") or "")
        if not base_url or not any(token in base_url for token in ("127.0.0.1", "localhost", "::1")):
            raise BridgeError(f"{job_id}: server_openai requires a loopback base_url")

    workspace_value = job.get("workspace") or state.get("workspace")
    if not workspace_value:
        raise BridgeError(f"{job_id}: missing workspace")
    workspace = pathlib.Path(str(workspace_value)).expanduser().resolve()
    remote_server = execution_transport == "ssh" and adapter in {"server_local", "server_openai"}
    remote_workspace: str | None = None
    prompt_file: str | None
    if remote_server:
        host_row = hosts.get(execution_host) or {}
        remote_root = str(host_row.get("project_path") or "")
        remote_workspace = _require_remote_path(
            str(job.get("remote_workspace") or spec.get("remote_workspace") or remote_root),
            remote_root,
            "remote_workspace",
        )
        if adapter == "server_local":
            remote_prompt = job.get("remote_prompt_file") or spec.get("remote_prompt_file")
            prompt_file = (
                _require_remote_path(remote_prompt, remote_workspace, "remote_prompt_file")
                if remote_prompt
                else None
            )
        else:
            # server_openai sends a small prompt payload over the authenticated
            # SSH stdin, so its source remains on the controller workspace.
            prompt_file = _require_path(
                job.get("prompt_file") or spec.get("prompt_file"), workspace, "prompt_file"
            )
        raw_artifacts = job.get("remote_required_artifacts") or _artifact_list(job, assignment)
        artifacts = [_require_remote_path(value, remote_workspace, "remote_required_artifact") for value in raw_artifacts]
        remote_result = (
            job.get("remote_result_source_path")
            or spec.get("remote_result_source_path")
            or job.get("result_source_path")
            or spec.get("result_source_path")
        )
        result_source = _require_remote_path(remote_result, remote_workspace, "remote_result_source_path")
        # A generic SSH command is expected to create the remote artifact.  A
        # local controller must never try to publish stdout to that path.
        output_path = None
    else:
        prompt_file = _require_path(job.get("prompt_file") or spec.get("prompt_file"), workspace, "prompt_file")
        artifacts = [_require_path(value, workspace, "required_artifact") for value in _artifact_list(job, assignment)]
        result_source = _require_path(
            job.get("result_source_path") or spec.get("result_source_path"), workspace, "result_source_path"
        )
        output_path = _require_path(
            job.get("output_path") or spec.get("output_path") or result_source, workspace, "output_path"
        )
    validation_argv = _coerce_validation(job, spec)
    if execution_transport == "ssh" and pathlib.Path(validation_argv[0]).is_absolute():
        raise BridgeError(
            f"{job_id}: remote validation must use a host-resolved executable name or remote absolute path"
        )

    attempt_id = "attempt-" + _canonical_digest(
        {"plan": plan_digest, "job_id": job_id, "pool_id": pool_id, "model": model, "variant": assignment.get("variant")}
    )[:16]
    attempt: dict[str, Any] = {
        "attempt_id": attempt_id,
        "adapter": adapter,
        "transport": execution_transport,
        "host_id": execution_host or None,
        "pool_id": pool_id,
        "provider": provider,
        "model": model,
        "variant": assignment.get("variant"),
        "prompt_file": prompt_file,
        "result_source_path": result_source,
        "output_path": output_path,
        "timeout_seconds": min(int(job.get("timeout_seconds", spec.get("timeout_seconds", 3600))), 86400),
        "validation_argv": validation_argv,
    }
    if remote_workspace and adapter == "server_local":
        attempt["workspace"] = remote_workspace
        attempt["remote_workspace"] = remote_workspace
        attempt["remote_result_source_path"] = result_source
    elif remote_workspace:
        attempt["remote_workspace"] = remote_workspace
        attempt["remote_result_source_path"] = result_source
    if adapter in {"command", "server_local"}:
        attempt["argv"] = _expand_argv(
            spec.get("argv"), assignment, pathlib.Path(remote_workspace or workspace)
        )
    for key in ("base_url", "temperature", "auto_approve", "pure", "print_timeout", "idle_timeout"):
        if key in spec:
            attempt[key] = spec[key]

    packet = {
        "schema_version": SCHEMA_VERSION,
        "packet_id": "packet-" + _canonical_digest({"plan": plan_digest, "job_id": job_id})[:16],
        "job_id": job_id,
        "pool_id": pool_id,
        "model": model,
        "variant": assignment.get("variant"),
        "plan_digest": plan_digest,
        "assignment_digest": _canonical_digest(assignment),
        "workspace": remote_workspace if remote_server and adapter == "server_local" else str(workspace),
        "write_scope": str(assignment.get("write_scope") or job.get("write_scope") or ""),
        "required_artifacts": artifacts,
        "validation_argv": validation_argv,
        "validation_required": True,
        "execution_host": execution_host,
        "workload_host": workload_host,
        "execution_transport": execution_transport,
        "workload_transport": workload_transport,
        "data_route": assignment.get("data_route"),
        "resource_request": assignment.get("resource_request") or {},
        "attempts": [attempt],
    }
    if remote_workspace and adapter == "server_openai":
        packet["remote_workspace"] = remote_workspace
    if not packet["write_scope"]:
        raise BridgeError(f"{job_id}: write_scope is required")
    return packet


def bridge_plan(
    plan: dict[str, Any],
    jobs: Any,
    state: dict[str, Any],
    adapter_registry: dict[str, Any],
    *,
    mode: str = "dry-run",
) -> dict[str, Any]:
    """Return packets for dispatch assignments without executing them."""
    if mode not in {"dry-run", "enqueue-ready"}:
        raise BridgeError("mode must be dry-run or enqueue-ready")
    if not isinstance(plan, dict) or plan.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise BridgeError("plan schema_version must be 1")
    if not plan.get("ok") or plan.get("decision") != "dispatch":
        raise BridgeError("only an ok dispatch plan can be bridged")
    assignments = plan.get("assignments")
    if not isinstance(assignments, list):
        raise BridgeError("plan assignments must be a list")
    by_id = _jobs_by_id(jobs)
    plan_digest = _canonical_digest(plan)
    packets: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for assignment in assignments:
        if not isinstance(assignment, dict):
            errors.append({"job_id": "", "error": "assignment must be an object"})
            continue
        job_id = str(assignment.get("job_id") or "")
        job = by_id.get(job_id)
        if job is None:
            errors.append({"job_id": job_id, "error": "assignment references unknown job"})
            continue
        if str(job.get("status") or "") in {"active", "running", "completed", "blocked"}:
            errors.append({"job_id": job_id, "error": "job is not enqueueable in its current status"})
            continue
        try:
            packets.append(assignment_to_packet(assignment, job, state, adapter_registry, plan_digest=plan_digest))
        except BridgeError as exc:
            errors.append({"job_id": job_id, "error": str(exc)})
    return {
        "schema_version": SCHEMA_VERSION,
        "bridge_version": "0.1.0",
        "mode": mode,
        "read_only": True,
        "ok": not errors and bool(packets),
        "plan_digest": plan_digest,
        "packets": packets,
        "errors": errors,
        "deferred": plan.get("deferred") or [],
    }


def enqueue_packets(report: dict[str, Any], db_path: pathlib.Path) -> dict[str, Any]:
    """Explicitly enqueue bridged packets into the local SQLite controller.

    Bridging remains read-only by default.  This function is the deliberately
    narrow mutation boundary used only by the ``--enqueue``/``--execute`` CLI
    flags: it validates that the report was built in ``enqueue-ready`` mode,
    delegates packet validation to :class:`SQLiteController`, and returns only
    redacted job summaries.  It never claims to execute a provider, starts no
    worker, and opens no network or SSH connection.
    """
    if not isinstance(report, dict):
        raise BridgeError("bridge report must be an object")
    if report.get("mode") != "enqueue-ready":
        raise BridgeError("SQLite enqueue requires bridge mode enqueue-ready")
    if not report.get("ok"):
        raise BridgeError("cannot enqueue a bridge report with validation errors")
    packets = report.get("packets")
    if not isinstance(packets, list) or not packets:
        raise BridgeError("bridge report has no enqueueable packets")
    path = pathlib.Path(db_path).expanduser().resolve()
    try:
        from sqlite_controller import SQLiteController
    except ImportError:  # pragma: no cover - package-style fallback
        from .sqlite_controller import SQLiteController  # type: ignore

    controller = SQLiteController(path)
    jobs: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for packet in packets:
        job_id = str(packet.get("job_id") or "") if isinstance(packet, dict) else ""
        if not job_id:
            errors.append({"job_id": "", "error": "packet is missing job_id"})
            continue
        try:
            row = controller.enqueue(packet)
            jobs.append(
                {
                    "job_id": row.get("job_id", job_id),
                    "status": row.get("status", "queued"),
                    "state_revision": row.get("state_revision"),
                }
            )
        except Exception as exc:
            # Keep provider/argv/prompt payloads out of the audit response.
            errors.append({"job_id": job_id, "error": f"{type(exc).__name__}: {exc}"})
    return {
        "schema_version": SCHEMA_VERSION,
        "backend": "sqlite",
        "db_path": str(path),
        "enqueue_requested": True,
        "enqueue_performed": bool(jobs),
        "ok": not errors and bool(jobs),
        "jobs": jobs,
        "errors": errors,
        "provider_execution": False,
        "model_prompts_sent": False,
    }


def load_json(path: pathlib.Path) -> Any:
    return json.loads(path.expanduser().read_text(encoding="utf-8"))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--jobs", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--adapters", required=True)
    parser.add_argument("--output")
    parser.add_argument("--mode", choices=("dry-run", "enqueue-ready"), default="dry-run")
    parser.add_argument(
        "--enqueue", "--execute", dest="enqueue", action="store_true",
        help="explicitly enqueue the validated packets into SQLite; never executes a provider",
    )
    parser.add_argument("--db", help="SQLite database path required with --enqueue/--execute")
    args = parser.parse_args(argv)
    try:
        if args.enqueue and not args.db:
            raise BridgeError("--enqueue/--execute requires --db")
        mode = "enqueue-ready" if args.enqueue else args.mode
        report = bridge_plan(
            load_json(pathlib.Path(args.plan)),
            load_json(pathlib.Path(args.jobs)),
            load_json(pathlib.Path(args.state)),
            load_json(pathlib.Path(args.adapters)),
            mode=mode,
        )
        if args.enqueue:
            enqueue_report = enqueue_packets(report, pathlib.Path(args.db))
            report = dict(report)
            report["read_only"] = False
            report["enqueue"] = enqueue_report
            report["enqueue_requested"] = True
            report["enqueue_performed"] = bool(enqueue_report.get("enqueue_performed"))
            report["provider_execution"] = False
            report["ok"] = bool(report.get("ok") and enqueue_report.get("ok"))
    except Exception as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "read_only": not bool(getattr(args, "enqueue", False)),
            "enqueue_requested": bool(getattr(args, "enqueue", False)),
            "enqueue_performed": False,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if args.output:
        pathlib.Path(args.output).expanduser().write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
