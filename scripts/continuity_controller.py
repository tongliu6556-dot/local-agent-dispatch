#!/usr/bin/env python3
"""Run a persistent, resumable job queue independently of a Codex chat session."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from urllib.parse import urlsplit
from typing import Any

try:
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt  # type: ignore[import-not-found]
except ImportError:  # POSIX
    msvcrt = None  # type: ignore[assignment]


QUOTA_RE = re.compile(
    r"quota|rate.?limit|usage.?limit|too many requests|insufficient balance|429",
    re.I,
)
AUTH_RE = re.compile(r"unauthorized|forbidden|sign.?in|auth|credential|401|403", re.I)
NETWORK_RE = re.compile(r"timeout|timed out|connection|network|tls|dns|unreachable", re.I)
CAPABILITY_RE = re.compile(r"cannot use this model|unsupported model|not entitled|model.*not found", re.I)
DEFAULT_ROOT = pathlib.Path(
    os.environ.get(
        "LOCAL_AGENT_DISPATCH_HOME",
        str(pathlib.Path.home() / ".codex" / "local-agent-dispatch"),
    )
)
DEFAULT_RUNTIME_STATE = DEFAULT_ROOT / "runtime-state.json"
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent

# Lazy import to keep the module loadable even if the helper is not yet present.
_process_group_run = None


def _load_process_group_run():
    """Import the process-group execution helper on first use."""
    global _process_group_run
    if _process_group_run is not None:
        return _process_group_run
    import importlib.util

    helper_path = SCRIPT_DIR / "process_group_run.py"
    spec = importlib.util.spec_from_file_location("process_group_run", helper_path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise RuntimeError(f"cannot load process-group helper: {helper_path}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses (used by ProcessGroupResult) expects the module to be
    # registered while executing a dynamically loaded module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _process_group_run = module
    return module


def now() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).isoformat()


def load_json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write(path: pathlib.Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            os.chmod(handle.name, 0o600)
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


def append_event(run_dir: pathlib.Path, event: str, **fields: Any) -> None:
    row = {
        "schema_version": 1,
        "event_id": uuid.uuid4().hex,
        "at_utc": now(),
        "event": event,
        **fields,
    }
    events_path = run_dir / "events.jsonl"
    with runtime_state_lock(events_path):
        with events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def state_path(run_dir: pathlib.Path) -> pathlib.Path:
    return run_dir / "state.json"


def load_state(run_dir: pathlib.Path) -> dict[str, Any]:
    payload = load_json(state_path(run_dir))
    if not isinstance(payload, dict):
        raise ValueError("continuity state must be an object")
    return payload


def validate_task_packet(job: dict[str, Any]) -> dict[str, Any]:
    """Validate a queued task packet before it becomes durable work.

    Planner-produced packets use the strict contract (``packet_id`` plus a
    non-empty ``attempts`` list).  The JSON controller is still a migration
    path for old hand-written queues, but that escape hatch must be explicit
    via ``legacy_compatibility=true``; malformed modern packets fail closed.
    The schema helper also rejects secret-like fields before they reach the
    state file or event log.
    """
    if not isinstance(job, dict):
        raise ValueError("task packet must be an object")
    try:
        import dispatch_schema

        dispatch_schema.validate("task_packet", job)
    except ImportError as exc:  # pragma: no cover - direct package embedding
        raise RuntimeError("task packet schema validator is unavailable") from exc
    except Exception as exc:
        raise ValueError(f"task packet schema validation failed: {exc}") from exc

    job_id = str(job.get("job_id") or "")
    if not job_id:
        raise ValueError("task packet must contain job_id")
    attempts = job.get("attempts")
    if attempts is None:
        if job.get("legacy_compatibility") is True:
            return {"mode": "legacy", "reason": "explicit legacy_compatibility"}
        raise ValueError("task packet requires a non-empty attempts list")
    if not isinstance(job.get("packet_id"), str) or not job.get("packet_id"):
        raise ValueError("modern task packet requires packet_id")
    if not isinstance(attempts, list) or not attempts:
        raise ValueError("task packet attempts must be a non-empty list")
    artifacts = job.get("required_artifacts")
    if not isinstance(artifacts, list) or not artifacts or not all(isinstance(item, str) and item for item in artifacts):
        raise ValueError("modern task packet requires a non-empty required_artifacts list")
    if not isinstance(job.get("write_scope"), str) or not job.get("write_scope"):
        raise ValueError("modern task packet requires write_scope")
    if job.get("validation_required") is not True:
        raise ValueError("modern task packet requires validation_required=true")
    if not (
        job.get("validation_argv")
        or job.get("validation_command")
        or any(attempt.get("validation_argv") or attempt.get("validation_command") for attempt in attempts)
    ):
        raise ValueError("modern task packet requires an explicit validator")
    for index, attempt in enumerate(attempts):
        if not isinstance(attempt, dict):
            raise ValueError(f"task packet attempt {index} must be an object")
        for field in ("attempt_id", "adapter", "transport", "model"):
            if not isinstance(attempt.get(field), str) or not attempt.get(field):
                raise ValueError(f"task packet attempt {index} requires {field}")
        if attempt.get("transport") not in {"local", "ssh"}:
            raise ValueError(f"task packet attempt {index} has unsupported transport")
        if attempt.get("adapter") == "command":
            argv = attempt.get("argv")
            if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
                raise ValueError(f"task packet attempt {index} command adapter requires argv")
        if "prompt" in attempt or "prompt" in job:
            raise ValueError("modern task packets must use prompt_file, not inline prompt text")
        # Reuse the shell-free validator contract for any supplied validator.
        _validation_argv(job, attempt)
    return {"mode": "strict", "attempt_count": len(attempts)}


def save_state(run_dir: pathlib.Path, state: dict[str, Any]) -> None:
    # Every persisted state is versioned, including states created by older
    # callers that predate the schema contract.  Do this at the last write
    # boundary so all controller paths get the same invariant.
    state.setdefault("schema_version", 1)
    state["state_revision"] = int(state.get("state_revision") or 0) + 1
    state["updated_at_utc"] = now()
    path = state_path(run_dir)
    with runtime_state_lock(path):
        atomic_write(path, state)


def classify(text: str, timed_out: bool = False) -> str:
    if timed_out:
        return "stall"
    if QUOTA_RE.search(text):
        return "quota"
    if CAPABILITY_RE.search(text):
        return "capability"
    if AUTH_RE.search(text):
        return "auth"
    if NETWORK_RE.search(text):
        return "network"
    return "execution"


def resolve_path(value: str, workspace: pathlib.Path) -> pathlib.Path:
    """Resolve a task path and reject traversal/symlink escape.

    Task packets may contain absolute paths for compatibility, but the
    canonical target must still remain below the declared workspace.  Using
    ``resolve(strict=False)`` catches both ``..`` traversal and existing
    symlinks while allowing a new output file to be created.
    """
    root = workspace.expanduser().resolve()
    path = pathlib.Path(value).expanduser()
    candidate = path if path.is_absolute() else root / path
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes workspace: {value}") from exc
    return resolved


@contextlib.contextmanager
def controller_lease(
    run_dir: pathlib.Path, owner_id: str | None = None, lease_ttl_seconds: int = 90
):
    """Hold an exclusive run-level lease for one controller process.

    The OS lock prevents two live controllers from consuming the same queue;
    the adjacent JSON record provides an auditable owner/heartbeat and is
    safely replaceable after a crash because the kernel releases the lock.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = run_dir / "controller.lock"
    lease_path = run_dir / "controller.lease.json"
    owner = owner_id or f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
    ttl = max(30, int(lease_ttl_seconds))
    handle = lock_path.open("a+", encoding="utf-8")

    def acquire() -> None:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("controller lease is already held") from exc
            return
        if msvcrt is not None:  # pragma: no cover - exercised on Windows CI
            handle.seek(0)
            if handle.read(1) == "":
                handle.seek(0)
                handle.write("\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("controller lease is already held") from exc
            return
        raise RuntimeError("no supported controller lease implementation")

    try:
        acquire()
    except Exception:
        handle.close()
        raise

    def heartbeat() -> dict[str, Any]:
        row = {
            "schema_version": 1,
            "owner_id": owner,
            "pid": os.getpid(),
            "heartbeat_at_utc": now(),
            "lease_expires_at_utc": (
                dt.datetime.now(tz=dt.timezone.utc) + dt.timedelta(seconds=ttl)
            ).isoformat(),
        }
        atomic_write(lease_path, row)
        return row

    heartbeat()
    stop_heartbeat = threading.Event()

    def heartbeat_loop() -> None:
        while not stop_heartbeat.wait(30):
            heartbeat()

    heartbeat_thread = threading.Thread(
        target=heartbeat_loop, name="local-agent-dispatch-lease-heartbeat", daemon=True
    )
    heartbeat_thread.start()
    try:
        yield heartbeat
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=2)
        try:
            lease_path.unlink(missing_ok=True)
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows CI
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            handle.close()


def file_fact(path: pathlib.Path) -> dict[str, Any]:
    try:
        stat = path.stat()
        return {"exists": True, "size": stat.st_size, "mtime": stat.st_mtime_ns}
    except OSError:
        return {"exists": False, "size": 0, "mtime": None}


def file_is_fresh(path: pathlib.Path, before: dict[str, Any]) -> bool:
    after = file_fact(path)
    return bool(
        after["exists"]
        and after["size"] > 0
        and (
            not before.get("exists")
            or after["size"] != before.get("size")
            or after["mtime"] != before.get("mtime")
        )
    )


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def publish_attempt_output(
    raw_output: str,
    output_path: str,
    result_source_path: str | None,
    workspace: pathlib.Path,
    result_source_before: dict[str, Any] | None,
) -> pathlib.Path:
    """Publish a clean final result while retaining raw CLI output in the log."""
    target = resolve_path(output_path, workspace)
    text = raw_output
    if result_source_path:
        source = resolve_path(result_source_path, workspace)
        if not file_is_fresh(source, result_source_before or file_fact(source)):
            raise RuntimeError(f"result source missing, empty, or stale: {source}")
        text = source.read_text(encoding="utf-8")
        if not text.strip():
            raise RuntimeError(f"result source is empty: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def runtime_state_path_for(
    state: dict[str, Any], job: dict[str, Any], attempt: dict[str, Any]
) -> pathlib.Path | None:
    value = attempt.get("runtime_state_path") or job.get("runtime_state_path") or state.get("runtime_state")
    return pathlib.Path(str(value)).expanduser() if value else None


@contextlib.contextmanager
def runtime_state_lock(path: pathlib.Path):
    """Serialize cross-controller read/modify/write updates."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:
            handle.seek(0)
            if handle.read(1) == "":
                handle.write("\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:  # pragma: no cover - supported CPython platforms expose one
            raise RuntimeError("no supported file-lock implementation")
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def runtime_reason(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " | ".join(lines[-8:])[-1200:] or "no diagnostic text"


def _redact_argv_for_log(argv: list[str]) -> str:
    """Produce a bounded, prompt-safe representation of a command argv for logs.

    Redacts any argument that looks like it could be inline prompt text
    (anything longer than 200 characters) to prevent leaking task content
    into diagnostic output.
    """
    parts: list[str] = []
    for item in argv:
        if len(item) > 200:
            parts.append(f"<redacted {len(item)} chars>")
        else:
            parts.append(item)
    return " ".join(parts)[-800:]


def record_runtime_feedback(
    state: dict[str, Any],
    job: dict[str, Any],
    attempt: dict[str, Any],
    *,
    success: bool,
    error_class: str | None,
    output: str,
) -> None:
    path = runtime_state_path_for(state, job, attempt)
    pool_id = str(attempt.get("pool_id") or job.get("pool_id") or "")
    if path is None or not pool_id:
        return
    with runtime_state_lock(path):
        _record_runtime_feedback_unlocked(
            state,
            job,
            attempt,
            success=success,
            error_class=error_class,
            output=output,
        )


def _record_runtime_feedback_unlocked(
    state: dict[str, Any],
    job: dict[str, Any],
    attempt: dict[str, Any],
    *,
    success: bool,
    error_class: str | None,
    output: str,
) -> None:
    """Persist invocation evidence so catalog refreshes cannot erase it."""
    path = runtime_state_path_for(state, job, attempt)
    pool_id = str(attempt.get("pool_id") or job.get("pool_id") or "")
    if path is None or not pool_id:
        return
    try:
        payload = load_json(path) if path.exists() else {}
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("schema_version", 1)
    pools = payload.setdefault("pools", {})
    pool = pools.setdefault(pool_id, {})
    observed_at = now()
    provider = str(attempt.get("provider") or job.get("provider") or pool_id.split(".", 1)[0])
    model = str(attempt.get("model") or job.get("model") or "")
    variant = str(attempt.get("variant") or job.get("variant") or "") or None

    if success:
        pool.update(
            health="ready",
            runtime_state="accepted",
            runtime_reason=None,
            last_runtime_success=observed_at,
            last_checked_at=observed_at,
        )
        pool.pop("cooldown_until_utc", None)
    elif str(error_class) == "capability":
        # A provider can reject one exact model/variant while sibling models
        # in the same subscription pool remain healthy.  Keep this evidence
        # model-scoped so a catalog refresh cannot poison the whole pool.
        pool.update(
            last_checked_at=observed_at,
            last_model_capability_failure=observed_at,
        )
    else:
        health = {
            "quota": "cooldown",
            "auth": "blocked",
            "network": "degraded",
            "capability": "degraded",
            "stall": "degraded",
        }.get(str(error_class), "degraded")
        pool.update(
            health=health,
            runtime_state="rejected",
            runtime_reason=runtime_reason(output),
            last_runtime_failure=observed_at,
            last_checked_at=observed_at,
            error_class=error_class,
        )
        cooldown = attempt.get("cooldown_until_utc") or job.get("cooldown_until_utc")
        if cooldown:
            pool["cooldown_until_utc"] = str(cooldown)

    if model:
        provider_models = payload.setdefault("models", {}).setdefault(provider, {})
        model_row = provider_models.setdefault(model, {})
        if success or str(error_class) == "capability":
            target_row = model_row
            if variant:
                target_row = model_row.setdefault("variants", {}).setdefault(variant, {})
                target_row["variant"] = variant
            target_row.update(
                runtime_state="accepted" if success else "rejected",
                runtime_reason=None if success else runtime_reason(output),
                last_checked_at=observed_at,
            )
        else:
            target_row = model_row
            target_row.update(
                last_checked_at=observed_at,
                last_runtime_failure=observed_at,
                last_failure_class=error_class,
                last_failure_reason=runtime_reason(output),
            )
        if success:
            target_row["last_runtime_success"] = observed_at
            # A successful variant is also positive evidence for the base
            # model, but the reverse is not assumed for rejected variants.
            model_row["runtime_state"] = "accepted"
            model_row["last_runtime_success"] = observed_at
        elif str(error_class) == "capability":
            target_row["last_runtime_failure"] = observed_at
            target_row["error_class"] = error_class
    payload["updated_at_utc"] = observed_at
    atomic_write(path, payload)


def load_inventory(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    inventory_path = pathlib.Path(str(state["inventory"])).expanduser()
    payload = load_json(inventory_path)
    hosts = payload.get("hosts", payload) if isinstance(payload, dict) else payload
    if isinstance(hosts, list):
        return {str(row["host_id"]): row for row in hosts}
    if isinstance(hosts, dict):
        return {str(key): dict(value or {}, host_id=key) for key, value in hosts.items()}
    raise ValueError("host inventory must contain a hosts list or object")


def ssh_argv(host: dict[str, Any], timeout: int = 10) -> list[str]:
    hostname = str(host.get("hostname") or "")
    if not hostname or any(char in hostname for char in "\n\r\0"):
        raise ValueError("SSH attempt requires a safe inventory hostname")
    user = str(host.get("user") or "")
    target = f"{user}@{hostname}" if user else hostname
    argv = [
        "ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={max(1, timeout)}",
        "-o", "ConnectionAttempts=3", "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=3",
    ]
    if host.get("port"):
        argv.extend(["-p", str(int(host["port"]))])
    if host.get("identity_file"):
        argv.extend(["-i", str(pathlib.Path(str(host["identity_file"])).expanduser())])
    argv.append(target)
    return argv


def prompt_text(job: dict[str, Any], attempt: dict[str, Any], workspace: pathlib.Path) -> str:
    if attempt.get("prompt") is not None:
        return str(attempt["prompt"])
    if job.get("prompt") is not None:
        return str(job["prompt"])
    value = attempt.get("prompt_file") or job.get("prompt_file")
    if not value:
        raise ValueError(f"job {job.get('job_id')} has no prompt or prompt_file")
    path = resolve_path(str(value), workspace)
    return path.read_text(encoding="utf-8")


def remote_confined_path(value: str, root: str, field: str = "remote_path") -> str:
    """Resolve a POSIX path below a declared remote project root.

    Remote paths are checked before they are embedded in the short Python
    request sent over SSH.  We reject traversal components instead of relying
    on lexical normalization; the remote observer also checks ``realpath`` so
    a symlink cannot escape the declared root after this check.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty path")
    if not isinstance(root, str) or not root.strip():
        raise ValueError("remote project root must be a non-empty path")
    raw = pathlib.PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in raw.parts if part != "/"):
        raise ValueError(f"{field} contains an unsafe path component")
    base = pathlib.PurePosixPath(root)
    if not base.is_absolute():
        raise ValueError("remote project root must be absolute")
    candidate = raw if raw.is_absolute() else base / raw
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"{field} escapes remote project root") from exc
    return str(candidate)


def validate_loopback_base_url(value: str) -> str:
    """Fail closed before a server-local request can become an SSRF path."""
    candidate = str(value or "").strip()
    parsed = urlsplit(candidate)
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise ValueError("server_openai base_url must be an http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError("server_openai base_url may not contain credentials")
    if hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("server_openai base_url must resolve to loopback")
    return candidate.rstrip("/")


def remote_openai_script(
    base_url: str,
    model: str,
    prompt: str,
    temperature: float,
    result_path: str | None = None,
) -> str:
    payload = json.dumps(
        {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": temperature},
        ensure_ascii=False,
    )
    result_literal = repr(result_path) if result_path else "None"
    script = f'''import json, os, pathlib, tempfile, urllib.request
payload = {payload!r}.encode("utf-8")
request = urllib.request.Request(
    {base_url.rstrip('/')!r} + "/chat/completions", data=payload,
    headers={{"Content-Type": "application/json"}}, method="POST")
with urllib.request.urlopen(request, timeout=3600) as response:
    result = json.loads(response.read().decode("utf-8"))
text = str(result["choices"][0]["message"]["content"])
result_path = {result_literal}
if result_path:
    target = pathlib.Path(result_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{{target.name}}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.write("\\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
print(text)
'''
    return script


def build_attempt(
    job: dict[str, Any], attempt: dict[str, Any], state: dict[str, Any], hosts: dict[str, dict[str, Any]]
) -> tuple[list[str], pathlib.Path | None, str | None, str | None]:
    adapter = str(attempt.get("adapter") or "command")
    workspace_value = str(attempt.get("workspace") or job.get("workspace") or state["workspace"])
    workspace = pathlib.Path(workspace_value).expanduser().resolve()
    transport = str(attempt.get("transport") or "local")
    host = None
    if transport == "ssh":
        host_id = str(attempt.get("host_id") or "")
        host = hosts.get(host_id)
        if not host:
            raise ValueError(f"unknown SSH host_id: {host_id}")

    output_path = attempt.get("output_path") or job.get("output_path")
    if adapter == "opencode":
        if transport != "local":
            raise ValueError("OpenCode Go adapter currently runs on its authenticated local host")
        prompt_file = attempt.get("prompt_file") or job.get("prompt_file")
        if not prompt_file:
            raise ValueError("OpenCode Go adapter requires prompt_file to keep task text out of process argv")
        prompt_path = resolve_path(str(prompt_file), workspace)
        result_source = attempt.get("result_source_path")
        if not result_source:
            raise ValueError("OpenCode Go adapter requires result_source_path")
        result_path = resolve_path(str(result_source), workspace)
        model = str(attempt.get("model") or job.get("model") or "")
        if not model:
            raise ValueError("OpenCode Go adapter requires an exact model from the persisted plan")
        argv = [
            sys.executable,
            str(SCRIPT_DIR / "opencode_guarded_run.py"),
            "--cwd",
            str(workspace),
            "--model",
            model,
            "--prompt-file",
            str(prompt_path),
            "--result-source",
            str(result_path),
        ]
        variant = attempt.get("variant") or job.get("variant")
        if variant:
            argv.extend(["--variant", str(variant)])
        if attempt.get("auto_approve"):
            argv.append("--auto-approve")
        if attempt.get("pure") is False:
            argv.append("--no-pure")
        return argv, workspace, str(output_path) if output_path else None, None

    if adapter == "cursor":
        if transport != "local":
            raise ValueError("cursor adapter currently runs on the local Mac")
        prompt = prompt_text(job, attempt, workspace)
        model = str(attempt.get("model") or "composer-2.5-fast")
        # NOTE: cursor-agent's existing CLI contract requires prompt via -p argv.
        # This is a known prompt-boundary limitation.  We emit a diagnostic
        # warning so callers can audit process listings, and _redact_argv_for_log
        # ensures the full prompt never appears in controller log diagnostics.
        if len(prompt) > 200:
            import warnings
            warnings.warn(
                "cursor adapter places prompt text in argv; "
                f"prompt length {len(prompt)} chars exceeds safe boundary — "
                "use prompt_file delivery when cursor-agent supports stdin",
                stacklevel=2,
            )
        return [
            "cursor-agent", "-p", prompt, "--trust", "--force",
            "--workspace", str(workspace), "--model", model,
        ], workspace, str(output_path) if output_path else None, None

    if adapter == "antigravity":
        if transport != "local":
            raise ValueError("antigravity adapter currently runs on the local Mac")
        prompt_file = attempt.get("prompt_file") or job.get("prompt_file")
        if not prompt_file:
            raise ValueError("antigravity adapter requires prompt_file")
        prompt_path = resolve_path(str(prompt_file), workspace)
        guarded = str(
            pathlib.Path(
                os.environ.get(
                    "ANTIGRAVITY_GUARDED_RUN",
                    str(
                        pathlib.Path.home()
                        / ".codex"
                        / "skills"
                        / "claude-usage-scheduler"
                        / "scripts"
                        / "antigravity_guarded_run.py"
                    ),
                )
            ).expanduser()
        )
        if not pathlib.Path(guarded).is_file():
            raise ValueError(
                "Antigravity adapter is unavailable; install/configure the optional "
                "guarded runner via ANTIGRAVITY_GUARDED_RUN"
            )
        argv = [
            sys.executable, guarded, "--cwd", str(workspace), "--prompt-file", str(prompt_path),
            "--model", str(attempt["model"]), "--print-timeout",
            str(attempt.get("print_timeout", "45m")), "--idle-timeout",
            str(int(attempt.get("idle_timeout", 150))),
        ]
        if attempt.get("watch_path"):
            argv.extend(["--watch-path", str(attempt["watch_path"])])
        if attempt.get("require_path"):
            argv.extend(["--require-path", str(attempt["require_path"])])
        if attempt.get("result_source_path"):
            result_source = resolve_path(str(attempt["result_source_path"]), workspace)
            argv.extend(["--child-stdout-file", str(result_source)])
        return argv, workspace, str(output_path) if output_path else None, None

    if adapter == "server_openai":
        base_url = validate_loopback_base_url(
            str(attempt.get("base_url") or "http://127.0.0.1:8000/v1")
        )
        prompt = prompt_text(job, attempt, workspace)
        if transport == "local":
            script = remote_openai_script(
                base_url,
                str(attempt["model"]), prompt, float(attempt.get("temperature", 0.1)),
            )
            return [sys.executable, "-c", script], workspace, str(output_path) if output_path else None, None
        if transport == "ssh" and host is not None:
            remote_root = str(host.get("project_path") or "")
            remote_workspace = str(
                attempt.get("remote_workspace")
                or job.get("remote_workspace")
                or remote_root
            )
            remote_workspace = remote_confined_path(remote_workspace, remote_root, "remote_workspace")
            remote_result = (
                attempt.get("remote_result_source_path")
                or job.get("remote_result_source_path")
                or attempt.get("result_source_path")
                or job.get("result_source_path")
            )
            if not remote_result:
                raise ValueError("server_openai SSH requires remote_result_source_path")
            remote_result = remote_confined_path(str(remote_result), remote_workspace, "remote_result_source_path")
            script = remote_openai_script(
                base_url,
                str(attempt["model"]), prompt, float(attempt.get("temperature", 0.1)),
                remote_result,
            )
            # The remote script writes the declared result artifact itself;
            # publishing stdout on the local controller would put a remote
            # path through local workspace confinement and could falsely mark
            # a split placement complete.
            return ssh_argv(host) + ["python3 -"], None, None, script
        raise ValueError("server_openai requires transport=local or transport=ssh with host_id")

    raw_argv = attempt.get("argv")
    if not isinstance(raw_argv, list) or not raw_argv or not all(isinstance(item, str) for item in raw_argv):
        raise ValueError(f"{adapter} adapter requires a non-empty string argv list")
    if transport == "local":
        return list(raw_argv), workspace, str(output_path) if output_path else None, None
    if transport == "ssh" and host is not None:
        remote_cwd = str(attempt.get("workspace") or job.get("remote_workspace") or host.get("project_path") or ".")
        remote_args = [remote_cwd, *raw_argv]
        remote = "sh -s -- " + " ".join(shlex.quote(item) for item in remote_args)
        runner = 'remote_cwd=$1\nshift\ncd "$remote_cwd"\nexec "$@"\n'
        return ssh_argv(host) + [remote], None, str(output_path) if output_path else None, runner
    raise ValueError(f"unsupported transport: {transport}")


def _validation_argv(job: dict[str, Any], attempt: dict[str, Any]) -> list[str] | None:
    """Return a shell-free validation argv from a task packet.

    ``validation_argv`` is preferred.  A legacy ``validation_command`` string
    is parsed with ``shlex`` and never passed through a shell, so operators do
    not accidentally grant arbitrary shell expansion to a task packet.
    """
    raw = attempt.get("validation_argv")
    if raw is None:
        raw = job.get("validation_argv")
    if raw is None:
        raw = attempt.get("validation_command")
    if raw is None:
        raw = job.get("validation_command")
    if raw is None:
        return None
    if isinstance(raw, str):
        argv = shlex.split(raw)
    elif isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        argv = list(raw)
    else:
        raise ValueError("validation command must be a string or string argv list")
    if not argv:
        raise ValueError("validation command must not be empty")
    executable = pathlib.Path(argv[0]).name.lower()
    if executable in {"sh", "bash", "zsh", "fish", "cmd", "powershell", "pwsh"} and any(
        item in {"-c", "/c", "-command"} for item in argv[1:]
    ):
        raise ValueError("shell validation is not allowed; use an explicit argv executable")
    return argv


def build_validation(
    job: dict[str, Any], attempt: dict[str, Any], state: dict[str, Any], hosts: dict[str, dict[str, Any]]
) -> tuple[list[str], pathlib.Path | None, str | None] | None:
    """Build a bounded local or SSH validation invocation."""
    argv = _validation_argv(job, attempt)
    if argv is None:
        return None
    workspace = pathlib.Path(
        str(attempt.get("workspace") or job.get("workspace") or state["workspace"])
    ).expanduser().resolve()
    transport = str(attempt.get("transport") or "local")
    if transport == "local":
        return argv, workspace, None
    if transport == "ssh":
        host_id = str(attempt.get("host_id") or "")
        host = hosts.get(host_id)
        if not host:
            raise ValueError(f"unknown SSH host_id for validation: {host_id}")
        remote_cwd = str(
            attempt.get("workspace") or job.get("remote_workspace") or host.get("project_path") or "."
        )
        remote_args = [remote_cwd, *argv]
        remote = "sh -s -- " + " ".join(shlex.quote(item) for item in remote_args)
        runner = 'remote_cwd=$1\nshift\ncd "$remote_cwd"\nexec "$@"\n'
        return ssh_argv(host) + [remote], None, runner
    raise ValueError(f"validation does not support transport={transport}")


def run_validation(
    job: dict[str, Any], attempt: dict[str, Any], state: dict[str, Any], hosts: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    spec = build_validation(job, attempt, state, hosts)
    if spec is None:
        return None
    argv, cwd, stdin_payload = spec
    timeout_seconds = max(1, int(attempt.get("validation_timeout_seconds", job.get("validation_timeout_seconds", 300))))
    pgrun = _load_process_group_run()
    result = pgrun.run_in_process_group(
        argv,
        cwd=str(cwd) if cwd else None,
        stdin_data=stdin_payload,
        timeout_seconds=timeout_seconds,
    )
    return {
        "ok": bool(result.returncode == 0 and not result.timed_out),
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "argv": _redact_argv_for_log(argv),
        "output": result.stdout[-8000:],
        "checked_at_utc": now(),
    }


def artifact_facts(job: dict[str, Any], workspace: pathlib.Path) -> list[dict[str, Any]]:
    facts = []
    for value in job.get("required_artifacts") or []:
        try:
            path = resolve_path(str(value), workspace)
            stat = path.stat()
            if not path.is_file():
                raise ValueError(f"required artifact is not a regular file: {path}")
            facts.append({
                "path": str(path),
                "exists": True,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "sha256": sha256_file(path),
            })
        except ValueError as exc:
            facts.append({"path": str(value), "exists": False, "size": 0, "mtime": None, "error": str(exc)})
        except OSError:
            # A missing artifact is the normal pre-execution baseline; it is
            # not an observation failure and must remain eligible for a later
            # freshness comparison.
            facts.append({"path": str(path), "exists": False, "size": 0, "mtime": None})
    return facts


def remote_artifact_facts(
    job: dict[str, Any], attempt: dict[str, Any], host: dict[str, Any]
) -> list[dict[str, Any]]:
    try:
        remote_root = remote_confined_path(
            str(attempt.get("remote_workspace") or attempt.get("workspace")
                or job.get("remote_workspace") or host.get("project_path") or ""),
            str(host.get("project_path") or ""),
            "remote_workspace",
        )
        paths = [
            remote_confined_path(str(value), remote_root, "required_artifact")
            for value in (job.get("required_artifacts") or [])
        ]
    except ValueError as exc:
        return [{
            "path": str(value),
            "exists": False,
            "size": 0,
            "mtime": None,
            "error": str(exc),
        } for value in (job.get("required_artifacts") or [])]
    if not paths:
        return []
    script = (
        "import hashlib,json,os,sys\n"
        "rows=[]\n"
        f"root={remote_root!r}\n"
        f"paths={paths!r}\n"
        "for p in paths:\n"
        " try:\n"
        "  real_root=os.path.realpath(root)\n"
        "  real=os.path.realpath(p)\n"
        "  if os.path.commonpath([real_root,real]) != real_root: raise OSError('path escapes remote project root')\n"
        "  s=os.stat(real)\n"
        "  if not os.path.isfile(real): raise OSError('not a regular file')\n"
        "  h=hashlib.sha256()\n"
        "  with open(real,'rb') as f:\n"
        "   for chunk in iter(lambda:f.read(1048576),b''): h.update(chunk)\n"
        "  rows.append({'path':real,'exists':True,'size':s.st_size,'mtime':s.st_mtime,'sha256':h.hexdigest()})\n"
        " except OSError:\n"
        "  rows.append({'path':p,'exists':False,'size':0,'mtime':None})\n"
        "print(json.dumps(rows))\n"
    )
    error = "remote artifact observation failed"
    for attempt_number in range(3):
        try:
            completed = subprocess.run(
                ssh_argv(host) + ["python3 -"], input=script, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=20, check=False,
            )
            if completed.returncode == 0:
                payload = json.loads(completed.stdout)
                if isinstance(payload, list):
                    return payload
            error = completed.stderr.strip()[-800:] or f"ssh exit {completed.returncode}"
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            error = str(exc)
        if attempt_number < 2:
            time.sleep(1)
    return [{"path": path, "exists": False, "size": 0, "mtime": None, "error": error} for path in paths]


def run_job(
    run_dir: pathlib.Path, job: dict[str, Any], state: dict[str, Any], hosts: dict[str, dict[str, Any]]
) -> None:
    attempts = job.get("attempts") or []
    if not attempts:
        job.update(status="failed", error="no attempts configured", finished_at_utc=now())
        return
    job["status"] = "running"
    job["started_at_utc"] = job.get("started_at_utc") or now()
    save_state(run_dir, state)
    append_event(run_dir, "job_started", job_id=job["job_id"])
    workspace = pathlib.Path(str(job.get("workspace") or state["workspace"])).expanduser()

    for index, attempt in enumerate(attempts):
        attempt_id = str(attempt.get("attempt_id") or f"attempt-{index + 1}")
        log_path = run_dir / "logs" / f"{job['job_id']}.{attempt_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        append_event(
            run_dir, "attempt_started", job_id=job["job_id"], attempt_id=attempt_id,
            adapter=attempt.get("adapter"), host_id=attempt.get("host_id"), model=attempt.get("model"),
        )
        timed_out = False
        # SSH server-local/OpenAI attempts produce and validate artifacts on
        # the workload host.  The old special case treated server_openai as
        # local even when its transport was SSH, which could mark a remote
        # call failed (or inspect the wrong local path).
        artifact_scope = str(attempt.get("artifact_scope") or attempt.get("transport") or "local")
        artifact_host = hosts.get(str(attempt.get("host_id") or "")) if artifact_scope == "ssh" else None
        before_facts = (
            remote_artifact_facts(job, attempt, artifact_host)
            if artifact_host
            else artifact_facts(job, workspace)
        )
        result_source_value = attempt.get("result_source_path")
        result_source_before = None
        if result_source_value and artifact_host is None:
            try:
                result_source_before = file_fact(resolve_path(str(result_source_value), workspace))
            except ValueError:
                # build_attempt will fail closed with the same confinement
                # error; do not let preflight of the stale source crash the
                # controller before an attempt record is written.
                result_source_before = None
        try:
            argv, cwd, output_path, stdin_payload = build_attempt(job, attempt, state, hosts)
            timeout_seconds = max(30, int(attempt.get("timeout_seconds", job.get("timeout_seconds", 3600))))
            pgrun = _load_process_group_run()
            pg_result = pgrun.run_in_process_group(
                argv,
                cwd=str(cwd) if cwd else None,
                stdin_data=stdin_payload,
                timeout_seconds=timeout_seconds,
            )
            output = pg_result.stdout or ""
            returncode = pg_result.returncode
            timed_out = pg_result.timed_out
            if timed_out:
                output += "\ncontinuity controller: attempt timed out\n"
                returncode = 124
                output_path = None
        except Exception as exc:
            output = f"continuity controller: {type(exc).__name__}: {exc}\n"
            returncode = 2
            output_path = None
        log_path.write_text(output, encoding="utf-8")
        if output_path and returncode == 0:
            try:
                publish_attempt_output(
                    output,
                    output_path,
                    str(result_source_value) if result_source_value else None,
                    workspace,
                    result_source_before,
                )
            except Exception as exc:
                output += f"\ncontinuity controller: {type(exc).__name__}: {exc}\n"
                log_path.write_text(output, encoding="utf-8")
                returncode = 2
        validation_result: dict[str, Any] | None = None
        if returncode == 0:
            try:
                validation_result = run_validation(job, attempt, state, hosts)
                if validation_result is None and job.get("validation_required") is True:
                    validation_result = {
                        "ok": False,
                        "returncode": 2,
                        "timed_out": False,
                        "output": "",
                        "error": "validation is required but no validator was configured",
                        "checked_at_utc": now(),
                    }
                if validation_result is not None:
                    output += (
                        "\ncontinuity validation "
                        f"returncode={validation_result['returncode']} "
                        f"timed_out={validation_result['timed_out']}\n"
                        f"{validation_result.get('output', '')}"
                    )
                    if not validation_result["ok"]:
                        returncode = int(validation_result["returncode"] or 2)
            except Exception as exc:
                validation_result = {
                    "ok": False,
                    "returncode": 2,
                    "timed_out": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "checked_at_utc": now(),
                }
                output += f"\ncontinuity validation failed: {validation_result['error']}\n"
                returncode = 2
        if validation_result is not None:
            job["last_validation"] = validation_result
        log_path.write_text(output, encoding="utf-8")
        if artifact_scope == "ssh":
            facts = remote_artifact_facts(job, attempt, artifact_host) if artifact_host else []
        else:
            facts = artifact_facts(job, workspace)
        before_by_path = {str(row.get("path")): row for row in before_facts}
        baseline_known = bool(facts) and all(not row.get("error") for row in before_facts)
        freshness_verified: bool | None = None if not facts else baseline_known and all(
            row.get("exists")
            and (
                not before_by_path.get(str(row.get("path")), {}).get("exists")
                or (
                    before_by_path.get(str(row.get("path")), {}).get("sha256")
                    and row.get("sha256")
                    and row.get("sha256")
                    != before_by_path.get(str(row.get("path")), {}).get("sha256")
                )
                or (
                    not before_by_path.get(str(row.get("path")), {}).get("sha256")
                    and (
                        row.get("size") != before_by_path.get(str(row.get("path")), {}).get("size")
                        or row.get("mtime") != before_by_path.get(str(row.get("path")), {}).get("mtime")
                    )
                )
            )
            for row in facts
        )
        if facts and job.get("accept_existing_artifacts"):
            # An existing artifact is not evidence that this attempt produced
            # it.  Permit the explicit legacy escape hatch only when an
            # independent validator has passed; otherwise keep the completion
            # gate closed so a stale file cannot masquerade as new work.
            freshness_verified = bool(validation_result and validation_result.get("ok"))
        artifacts_ok = (
            all(row["exists"] and row["size"] > 0 and row.get("sha256") for row in facts)
            and freshness_verified
            if facts else returncode == 0
        )
        if returncode == 0 and artifacts_ok:
            record_runtime_feedback(
                state, job, attempt, success=True, error_class=None, output=output
            )
            job.update(
                status="completed", completed_attempt=attempt_id, finished_at_utc=now(),
                artifacts=facts, artifact_freshness_verified=freshness_verified,
                validation=validation_result, error=None,
            )
            save_state(run_dir, state)
            append_event(run_dir, "job_completed", job_id=job["job_id"], attempt_id=attempt_id, artifacts=facts)
            return
        error_class = classify(output, timed_out)
        record_runtime_feedback(
            state, job, attempt, success=False, error_class=error_class, output=output
        )
        history = job.setdefault("attempt_history", [])
        history.append(
            {"attempt_id": attempt_id, "returncode": returncode, "error_class": error_class,
             "log_path": str(log_path), "finished_at_utc": now(), "artifacts": facts,
             "artifact_freshness_verified": freshness_verified,
             "validation": validation_result}
        )
        save_state(run_dir, state)
        append_event(
            run_dir, "attempt_failed", job_id=job["job_id"], attempt_id=attempt_id,
            returncode=returncode, error_class=error_class, log_path=str(log_path),
        )
        allowed = set(attempt.get("fallback_on") or ["quota", "auth", "network", "capability"])
        if index + 1 >= len(attempts) or error_class not in allowed:
            job.update(
                status="failed", error=error_class, finished_at_utc=now(), artifacts=facts,
                artifact_freshness_verified=freshness_verified, validation=validation_result,
            )
            save_state(run_dir, state)
            append_event(run_dir, "job_failed", job_id=job["job_id"], error_class=error_class)
            return


def _command_init_unlocked(args: argparse.Namespace) -> int:
    run_dir = pathlib.Path(args.run_dir).expanduser()
    run_dir.mkdir(parents=True, exist_ok=True)
    path = state_path(run_dir)
    if path.exists() and not args.force:
        raise FileExistsError(f"state already exists: {path}")
    payload = {
        "schema_version": 1, "run_id": args.run_id or uuid.uuid4().hex[:12],
        "workspace": str(pathlib.Path(args.workspace).expanduser().resolve()),
        "inventory": str(pathlib.Path(args.inventory).expanduser().resolve()),
        "runtime_state": str(pathlib.Path(args.runtime_state).expanduser().resolve()),
        "status": "prepared", "created_at_utc": now(), "updated_at_utc": now(), "jobs": [],
    }
    atomic_write(path, payload)
    append_event(run_dir, "run_initialized", run_id=payload["run_id"], workspace=payload["workspace"])
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def command_init(args: argparse.Namespace) -> int:
    run_dir = pathlib.Path(args.run_dir).expanduser()
    with controller_lease(run_dir, owner_id=f"init-{os.getpid()}"):
        return _command_init_unlocked(args)


def _command_enqueue_unlocked(args: argparse.Namespace) -> int:
    run_dir = pathlib.Path(args.run_dir).expanduser()
    state = load_state(run_dir)
    job = load_json(pathlib.Path(args.job_file).expanduser())
    if not isinstance(job, dict) or not job.get("job_id"):
        raise ValueError("job file must contain an object with job_id")
    job_id = str(job["job_id"])
    if any(str(row.get("job_id")) == job_id for row in state["jobs"]):
        raise ValueError(f"duplicate job_id: {job_id}")
    # Queue entries are task packets on disk.  Version is backfilled before
    # validation so explicit legacy packets can migrate without ambiguity.
    job.setdefault("schema_version", 1)
    packet_validation = validate_task_packet(job)
    job["job_id"] = job_id
    job["status"] = "queued"
    job["queued_at_utc"] = now()
    job["packet_validation"] = packet_validation
    state["jobs"].append(job)
    state["status"] = "queued"
    save_state(run_dir, state)
    append_event(run_dir, "job_enqueued", job_id=job_id)
    print(json.dumps(job, ensure_ascii=False, indent=2))
    return 0


def command_enqueue(args: argparse.Namespace) -> int:
    run_dir = pathlib.Path(args.run_dir).expanduser()
    with controller_lease(run_dir, owner_id=f"enqueue-{os.getpid()}"):
        return _command_enqueue_unlocked(args)


def _command_run_loop(
    args: argparse.Namespace, run_dir: pathlib.Path, heartbeat: Any
) -> int:
    run_dir = pathlib.Path(args.run_dir).expanduser()
    idle_rounds = 0
    while True:
        heartbeat()
        state = load_state(run_dir)
        hosts = load_inventory(state)
        completed = {str(row["job_id"]) for row in state["jobs"] if row.get("status") == "completed"}
        runnable = [
            row for row in state["jobs"] if row.get("status") in {"queued", "retry"}
            and all(str(dep) in completed for dep in row.get("depends_on") or [])
        ]
        if runnable:
            idle_rounds = 0
            run_job(run_dir, runnable[0], state, hosts)
            heartbeat()
            if args.once:
                state = load_state(run_dir)
                unfinished_after = [
                    row for row in state["jobs"]
                    if row.get("status") not in {"completed", "failed", "blocked"}
                ]
                if not unfinished_after:
                    state["status"] = (
                        "completed"
                        if all(row.get("status") == "completed" for row in state["jobs"])
                        else "attention"
                    )
                    save_state(run_dir, state)
                    append_event(run_dir, "run_finished", status=state["status"])
                break
            continue
        unfinished = [
            row for row in state["jobs"]
            if row.get("status") not in {"completed", "failed", "blocked", "artifact_ready_needs_review"}
        ]
        if not unfinished:
            state["status"] = "completed" if all(row.get("status") == "completed" for row in state["jobs"]) else "attention"
            save_state(run_dir, state)
            append_event(run_dir, "run_finished", status=state["status"])
            break
        idle_rounds += 1
        if args.once or (args.max_idle_rounds and idle_rounds >= args.max_idle_rounds):
            break
        time.sleep(max(1, args.poll_seconds))
    return 0


def command_run(args: argparse.Namespace) -> int:
    run_dir = pathlib.Path(args.run_dir).expanduser()
    with controller_lease(run_dir, owner_id=getattr(args, "owner_id", None)) as heartbeat:
        return _command_run_loop(args, run_dir, heartbeat)


def status_payload(run_dir: pathlib.Path) -> dict[str, Any]:
    state = load_state(run_dir)
    lease_path = run_dir / "controller.lease.json"
    try:
        lease = load_json(lease_path) if lease_path.exists() else None
    except (OSError, ValueError):
        lease = {"status": "unreadable"}
    counts: dict[str, int] = {}
    for job in state.get("jobs", []):
        status = str(job.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {
        "run_id": state.get("run_id"), "status": state.get("status"),
        "workspace": state.get("workspace"), "updated_at_utc": state.get("updated_at_utc"),
        "counts": counts,
        "controller_lease": lease,
        "jobs": [
            {key: row.get(key) for key in (
                "job_id", "status", "completed_attempt", "error", "artifacts",
                "reconciled_artifacts", "reconciled_at_utc", "attempt_history"
            ) if row.get(key) is not None}
            for row in state.get("jobs", [])
        ],
        "events_path": str(run_dir / "events.jsonl"),
        "state_path": str(state_path(run_dir)),
    }


def command_status(args: argparse.Namespace) -> int:
    run_dir = pathlib.Path(args.run_dir).expanduser()
    payload = status_payload(run_dir)
    if args.output:
        atomic_write(pathlib.Path(args.output).expanduser(), payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _command_resume_unlocked(args: argparse.Namespace) -> int:
    """Reconcile durable artifacts without promoting them past a review gate."""
    run_dir = pathlib.Path(args.run_dir).expanduser()
    state = load_state(run_dir)
    hosts = load_inventory(state)
    changed = False
    for job in state.get("jobs", []):
        if job.get("status") == "completed" or not job.get("required_artifacts"):
            continue
        history = job.get("attempt_history") or []
        attempt_id = history[-1].get("attempt_id") if history else None
        attempts = job.get("attempts") or []
        attempt = next((row for row in attempts if row.get("attempt_id") == attempt_id), attempts[-1] if attempts else {})
        scope = str(attempt.get("artifact_scope") or attempt.get("transport") or "local")
        if scope == "ssh":
            host = hosts.get(str(attempt.get("host_id") or ""))
            facts = remote_artifact_facts(job, attempt, host) if host else []
        else:
            workspace = pathlib.Path(str(job.get("workspace") or state["workspace"])).expanduser()
            facts = artifact_facts(job, workspace)
        if facts and all(row.get("exists") and row.get("size", 0) > 0 and not row.get("error") for row in facts):
            job["reconciled_artifacts"] = facts
            job["status"] = "artifact_ready_needs_review"
            job["reconciled_at_utc"] = now()
            changed = True
            append_event(
                run_dir, "artifacts_reconciled_needs_review",
                job_id=job.get("job_id"), artifacts=facts,
            )
    if changed:
        state["status"] = "attention"
        save_state(run_dir, state)
    return command_status(args)


def command_resume(args: argparse.Namespace) -> int:
    run_dir = pathlib.Path(args.run_dir).expanduser()
    with controller_lease(run_dir, owner_id=f"resume-{os.getpid()}"):
        return _command_resume_unlocked(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--run-dir", required=True)
    init.add_argument("--workspace", required=True)
    init.add_argument("--inventory", default=str(DEFAULT_ROOT / "hosts.json"))
    init.add_argument("--runtime-state", default=str(DEFAULT_RUNTIME_STATE))
    init.add_argument("--run-id")
    init.add_argument("--force", action="store_true")
    init.set_defaults(handler=command_init)
    enqueue = sub.add_parser("enqueue")
    enqueue.add_argument("--run-dir", required=True)
    enqueue.add_argument("--job-file", required=True)
    enqueue.set_defaults(handler=command_enqueue)
    run = sub.add_parser("run")
    run.add_argument("--run-dir", required=True)
    run.add_argument("--poll-seconds", type=int, default=15)
    run.add_argument("--max-idle-rounds", type=int, default=0)
    run.add_argument("--once", action="store_true")
    run.add_argument("--owner-id", help="auditable controller lease owner id")
    run.set_defaults(handler=command_run)
    status = sub.add_parser("status")
    status.add_argument("--run-dir", required=True)
    status.add_argument("--output")
    status.set_defaults(handler=command_status)
    resume = sub.add_parser("resume")
    resume.add_argument("--run-dir", required=True)
    resume.add_argument("--output")
    resume.set_defaults(handler=command_resume)
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
