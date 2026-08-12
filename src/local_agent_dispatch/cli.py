"""Command-line entry point for local-agent-dispatch smoke tooling."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import datetime as dt
from pathlib import Path
from typing import Any

from . import __version__

RepoRoot = Path

ROOT_MARKERS = ("SKILL.md", "scripts/local_system_scan.py")
REQUIRED_OFFLINE_SCRIPTS = ("local_system_scan.py",)

OFFLINE_PROVIDERS = {
    "codex": "codex",
    "cursor": "cursor-agent",
    "antigravity": "antigravity",
    "opencode": "opencode",
}


def _candidate_repo_roots(start: Path | None = None) -> list[Path]:
    roots: list[Path] = []
    current = (start or Path(__file__).resolve()).resolve()
    if current.is_file():
        current = current.parent
    roots.append(current)
    roots.extend(current.parents)
    return roots


def locate_repo_root() -> Path:
    """Locate the repository root that contains the bundled scripts."""
    env_root = os.environ.get("LAD_REPO_ROOT")
    if env_root:
        candidate = Path(env_root).expanduser().resolve()
        if all((candidate / marker).exists() for marker in ROOT_MARKERS):
            return candidate
        raise FileNotFoundError(
            f"LAD_REPO_ROOT is set but missing expected markers in {candidate}"
        )

    for candidate in _candidate_repo_roots():
        if all((candidate / marker).exists() for marker in ROOT_MARKERS):
            return candidate

    # A wheel install keeps the thin CLI in site-packages and places the
    # read-only scripts under the PEP 517 data directory.  Resolve both a
    # normal venv install (`sysconfig.data/share`) and `pip --target` layouts
    # (`<target>/share`) without requiring a source checkout or env override.
    packaged_candidates: list[Path] = []
    data_root = sysconfig.get_path("data")
    if data_root:
        packaged_candidates.append(Path(data_root) / "share" / "local-agent-dispatch")
    current = Path(__file__).resolve()
    for parent in current.parents:
        packaged_candidates.append(parent / "share" / "local-agent-dispatch")
    for packaged in packaged_candidates:
        if all((packaged / marker).exists() for marker in ROOT_MARKERS):
            return packaged

    raise FileNotFoundError(
        "Could not locate local-agent-dispatch repository root. Set LAD_REPO_ROOT explicitly."
    )


def repo_script(name: str, repo_root: Path) -> Path:
    path = repo_root / "scripts" / name
    if not path.is_file():
        raise FileNotFoundError(
            f"Bundled script missing: {path.name}. Expected repository scripts in {repo_root}"
        )
    return path


def _safe_scan_environment() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().endswith("_KEY")}
    env.pop("OPENAI_API_KEY", None)
    env.pop("ANTIGRAVITY_TOKEN", None)
    env.pop("CURSOR_TOKEN", None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["NODE_DISABLE_COMPILE_CACHE"] = "1"
    return env


def _execution_environment() -> dict[str, str]:
    """Preserve user-configured provider credentials for explicit execution."""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["NODE_DISABLE_COMPILE_CACHE"] = "1"
    return env


def _run_json_script(
    path: Path, args: list[str], *, timeout_seconds: float = 20.0
) -> tuple[int, dict[str, Any]]:
    proc = subprocess.run(
        [sys.executable, str(path), *args],
        check=False,
        cwd=str(path.parent),
        env=_safe_scan_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=max(1.0, float(timeout_seconds)),
    )

    stdout = proc.stdout.strip()
    if proc.returncode != 0 and not stdout:
        return proc.returncode, {
            "ok": False,
            "error": f"{path.name} exited with {proc.returncode}",
            "stderr": proc.stderr.strip(),
        }

    if not stdout:
        return proc.returncode, {
            "ok": False,
            "error": f"{path.name} produced no JSON output",
            "stderr": proc.stderr.strip(),
        }

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return proc.returncode, {
            "ok": False,
            "error": f"{path.name} output is not valid JSON",
            "details": str(exc),
            "stderr": proc.stderr.strip(),
        }

    if not isinstance(payload, dict):
        payload = {"value": payload}
    payload.setdefault("ok", proc.returncode == 0)
    return proc.returncode, payload


def _provider_key(name: str) -> str:
    return OFFLINE_PROVIDERS.get(name.lower(), name)


def _expected_provider_status(scan: dict[str, Any], expected: list[str]) -> dict[str, Any]:
    clis = scan.get("clis", {}) if isinstance(scan, dict) else {}
    requested: list[str] = []
    available: list[str] = []
    missing: list[str] = []

    for item in expected:
        key = _provider_key(item)
        requested.append(item)
        present = bool(clis.get(key, {}).get("present"))
        if present:
            available.append(key)
        else:
            missing.append(item)

    status: dict[str, Any] = {
        "requested": sorted(set(requested)),
        "available": sorted(set(available)),
        "missing": sorted(set(missing)),
    }
    status["ok"] = not missing
    return status


def _run_scan_command(workspace: Path, expected_providers: list[str] | None = None) -> dict[str, Any]:
    repo_root = locate_repo_root()
    scan_script = repo_script("local_system_scan.py", repo_root)
    workspace = workspace.expanduser().resolve()
    if not workspace.exists():
        raise FileNotFoundError(f"Workspace does not exist: {workspace}")
    if not workspace.is_dir():
        raise NotADirectoryError(f"Workspace is not a directory: {workspace}")

    _, scan_payload = _run_json_script(
        scan_script,
        ["--workspace", str(workspace), "--compact"],
    )
    provider_status = _expected_provider_status(scan_payload, expected_providers or [])
    return {
        "repo_root": str(repo_root),
        "script": str(scan_script),
        "scan": scan_payload,
        "provider_check": provider_status,
    }


def _run_hardware_fit(preflight: Path, jobs: Path, workspace: Path) -> dict[str, Any]:
    """Render a read-only local-hardware/server-fit report from saved facts."""
    repo_root = locate_repo_root()
    script = repo_script("hardware_fit_planner.py", repo_root)
    for path, label in ((preflight, "preflight"), (jobs, "jobs")):
        if not path.is_file():
            raise FileNotFoundError(f"{label} file does not exist: {path}")
    _, payload = _run_json_script(
        script,
        [
            "--preflight", str(preflight.expanduser().resolve()),
            "--jobs", str(jobs.expanduser().resolve()),
            "--workspace", str(workspace.expanduser().resolve()),
        ],
    )
    payload.setdefault("evidence_level", "saved-preflight-only")
    payload.setdefault("model_prompt_sent", False)
    return payload


def _run_task_estimate(
    task: str, repo_root: Path | None, manifest: Path | None, history: Path | None
) -> tuple[int, dict[str, Any]]:
    """Build a provider-free P50/P90 task estimate for the planner gate."""
    script = repo_script("task_estimator.py", locate_repo_root())
    args = ["--task", task]
    if repo_root:
        args.extend(["--repo-root", str(repo_root.expanduser().resolve())])
    if manifest:
        args.extend(["--manifest", str(manifest.expanduser().resolve())])
    if history:
        args.extend(["--history", str(history.expanduser().resolve())])
    return _run_json_script(script, args, timeout_seconds=30.0)


def _run_task_capture(
    task: str,
    repo_root: Path | None,
    manifest: Path | None,
    git_metadata: Path | None,
    policy: Path | None,
    history: Path | None,
    model: str | None,
    host: str | None,
) -> tuple[int, dict[str, Any]]:
    """Capture a provider-free TaskPacket through the bundled script."""
    script = repo_script("task_capture.py", locate_repo_root())
    args = ["--task", task]
    for value, flag in (
        (repo_root, "--repo-root"),
        (manifest, "--manifest"),
        (git_metadata, "--git-metadata"),
        (policy, "--policy"),
        (history, "--history"),
    ):
        if value:
            args.extend([flag, str(value.expanduser().resolve())])
    for value, flag in ((model, "--model"), (host, "--host")):
        if value:
            args.extend([flag, value])
    return _run_json_script(script, args, timeout_seconds=30.0)


def _run_dispatch_workflow(
    workspace: Path,
    jobs: Path,
    preflight: Path | None,
    inventory: Path | None,
    model_state: Path | None,
    runtime_state: Path | None,
    manifest: Path | None,
    history: Path | None,
    output: Path | None,
    timeout_seconds: float,
    max_lanes: int,
    horizon: int,
    live_probes: bool,
    skip_antigravity_usage: bool,
) -> tuple[int, dict[str, Any]]:
    """Run the provider-free system-first dispatch workflow.

    A saved ``--preflight`` snapshot is the default and keeps this command
    fully offline with respect to providers.  ``--live-probes`` is an explicit
    opt-in for catalog/quota/SSH discovery; the workflow still never executes
    a provider or sends a model prompt.
    """

    repo_root = locate_repo_root()
    script = repo_script("dispatch_workflow.py", repo_root)
    workspace = workspace.expanduser().resolve()
    jobs = jobs.expanduser().resolve()
    if not workspace.is_dir():
        raise NotADirectoryError(f"workspace does not exist: {workspace}")
    if not jobs.is_file():
        raise FileNotFoundError(f"jobs file does not exist: {jobs}")
    args = [
        "--workspace", str(workspace),
        "--jobs", str(jobs),
        "--max-lanes", str(max(1, int(max_lanes))),
        "--horizon", str(max(1, int(horizon))),
        "--timeout", str(max(5.0, float(timeout_seconds))),
    ]
    for value, flag in (
        (preflight, "--preflight"),
        (inventory, "--inventory"),
        (model_state, "--model-state"),
        (runtime_state, "--runtime-state"),
        (manifest, "--manifest"),
        (history, "--history"),
        (output, "--output"),
    ):
        if value is not None:
            args.extend([flag, str(value.expanduser().resolve())])
    if live_probes:
        args.append("--live-probes")
    if skip_antigravity_usage:
        args.append("--skip-antigravity-usage")
    proc = subprocess.run(
        [sys.executable, str(script), *args],
        check=False,
        cwd=str(workspace),
        env=_execution_environment() if live_probes else _safe_scan_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=max(60.0, float(timeout_seconds) * 6.0 + 30.0),
    )
    stdout = proc.stdout.strip()
    if not stdout:
        # ``dispatch_workflow.py`` writes directly to ``--output`` when a
        # report path is supplied.  The thin CLI still needs to return that
        # same report on stdout for scripting and for callers that do not want
        # to read a second path, so recover the just-written JSON here.
        if output is not None and output.expanduser().is_file():
            try:
                stdout = output.expanduser().read_text(encoding="utf-8").strip()
            except OSError:
                stdout = ""
    if not stdout:
        return proc.returncode, {
            "ok": False,
            "error": "dispatch workflow produced no JSON output",
            "stderr": proc.stderr.strip(),
        }
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return proc.returncode, {
            "ok": False,
            "error": f"dispatch workflow output is not valid JSON: {exc}",
            "stderr": proc.stderr.strip(),
        }
    if not isinstance(payload, dict):
        payload = {"value": payload}
    payload.setdefault("ok", proc.returncode == 0)
    if proc.stderr.strip():
        payload.setdefault("stderr", proc.stderr.strip()[-2000:])
    return proc.returncode, payload


def _run_preflight(
    workspace: Path,
    inventory: Path,
    output: Path | None,
    model_state: Path | None,
    runtime_state: Path | None,
    timeout_seconds: float,
    skip_antigravity_usage: bool,
) -> tuple[int, dict[str, Any]]:
    """Run the explicit system-first/provider/compute preflight.

    Unlike offline scan commands this operation may contact configured CLIs and
    SSH hosts, so it deliberately preserves the execution environment.  The
    preflight script itself still performs the local hardware stage first and
    never sends a model prompt.
    """
    repo_root = locate_repo_root()
    script = repo_script("dispatch_preflight_scan.py", repo_root)
    workspace = workspace.expanduser().resolve()
    inventory = inventory.expanduser().resolve()
    if not workspace.is_dir():
        raise NotADirectoryError(f"workspace does not exist or is not a directory: {workspace}")
    if not inventory.is_file():
        raise FileNotFoundError(f"inventory file does not exist: {inventory}")
    args = [
        "--cwd", str(workspace),
        "--inventory", str(inventory),
        "--timeout", str(max(5.0, float(timeout_seconds))),
    ]
    if output is not None:
        args.extend(["--output", str(output.expanduser().resolve())])
    if model_state is not None:
        args.extend(["--model-state", str(model_state.expanduser().resolve())])
    if runtime_state is not None:
        args.extend(["--runtime-state", str(runtime_state.expanduser().resolve())])
    if skip_antigravity_usage:
        args.append("--skip-antigravity-usage")
    proc = subprocess.run(
        [sys.executable, str(script), *args],
        check=False,
        cwd=str(workspace),
        env=_execution_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=max(60.0, float(timeout_seconds) * 6.0 + 30.0),
    )
    stdout = proc.stdout.strip()
    try:
        payload = json.loads(stdout) if stdout else {
            "ok": False,
            "error": "preflight produced no JSON output",
            "stderr": proc.stderr.strip(),
        }
    except json.JSONDecodeError as exc:
        payload = {
            "ok": False,
            "error": f"preflight output is not valid JSON: {exc}",
            "stderr": proc.stderr.strip(),
        }
    if not isinstance(payload, dict):
        payload = {"ok": False, "error": "preflight output must be a JSON object"}
    payload.setdefault("evidence_level", "preflight-no-prompt")
    payload.setdefault("model_prompt_sent", False)
    return proc.returncode, payload


def _run_plan_bridge(
    plan: Path,
    jobs: Path,
    state: Path,
    adapters: Path,
    mode: str,
    *,
    enqueue: bool = False,
    db: Path | None = None,
) -> tuple[int, dict[str, Any]]:
    repo_root = locate_repo_root()
    script = repo_script("plan_packet_bridge.py", repo_root)
    for path, label in ((plan, "plan"), (jobs, "jobs"), (state, "state"), (adapters, "adapters")):
        if not path.is_file():
            raise FileNotFoundError(f"{label} file does not exist: {path}")
    if enqueue and db is None:
        raise ValueError("bridge --enqueue/--execute requires --db")
    args = [
        "--plan", str(plan.expanduser().resolve()),
        "--jobs", str(jobs.expanduser().resolve()),
        "--state", str(state.expanduser().resolve()),
        "--adapters", str(adapters.expanduser().resolve()),
        "--mode", mode,
    ]
    if enqueue:
        args.extend(["--enqueue", "--db", str(db.expanduser().resolve())])
    return _run_json_script(script, args)


def _enqueue_dispatch_report(
    report: dict[str, Any],
    *,
    jobs: Path,
    workspace: Path,
    preflight: Path | None,
    adapters: Path,
    db: Path,
) -> dict[str, Any]:
    """Bridge one provider-free dispatch report and enqueue it into SQLite.

    ``lad dispatch`` normally ends at a read-only report.  The caller must
    opt into this helper with ``--enqueue``/``--execute`` and an explicit
    ``--db`` plus adapter registry.  The helper imports the already audited
    bridge in-process, so no temporary plan or packet file is needed and no
    provider/SSH boundary is reachable from this path.
    """
    if not adapters.is_file():
        raise FileNotFoundError(f"adapters file does not exist: {adapters}")
    if not jobs.is_file():
        raise FileNotFoundError(f"jobs file does not exist: {jobs}")
    if preflight is not None and not preflight.is_file():
        raise FileNotFoundError(f"preflight file does not exist: {preflight}")
    repo_root = locate_repo_root()
    scripts_root = str(repo_root / "scripts")
    if scripts_root not in sys.path:
        sys.path.insert(0, scripts_root)
    import plan_packet_bridge as bridge  # type: ignore

    planner = report.get("planner")
    if not isinstance(planner, dict):
        raise ValueError("dispatch report has no planner plan to bridge")
    plan = dict(planner)
    plan["schema_version"] = int(plan.get("schema_version") or 1)
    plan["ok"] = bool(report.get("ok") and plan.get("ok"))
    plan["decision"] = str(plan.get("decision") or ("dispatch" if report.get("assignments") else "pause"))
    plan["assignments"] = list(report.get("assignments") or plan.get("assignments") or [])
    jobs_payload = json.loads(jobs.expanduser().read_text(encoding="utf-8"))
    adapters_payload = json.loads(adapters.expanduser().read_text(encoding="utf-8"))
    if preflight is not None:
        state_payload = json.loads(preflight.expanduser().read_text(encoding="utf-8"))
    else:
        # A live-probe report contains only redacted host/pool summaries.  That
        # is sufficient for bridge validation and avoids persisting probe data
        # or requiring a second state file for this explicit local enqueue.
        host_rows = report.get("hosts") if isinstance(report.get("hosts"), dict) else {}
        state_payload = {
            "schema_version": 1,
            "workspace": str(workspace.expanduser().resolve()),
            "hosts": host_rows,
            "compute_hosts": host_rows,
            "pools": report.get("pools") if isinstance(report.get("pools"), dict) else {},
        }
    bridged = bridge.bridge_plan(
        plan,
        jobs_payload,
        state_payload,
        adapters_payload,
        mode="enqueue-ready",
    )
    enqueue = bridge.enqueue_packets(bridged, db.expanduser().resolve())
    return {
        "schema_version": 1,
        "ok": bool(bridged.get("ok") and enqueue.get("ok")),
        "read_only": False,
        "provider_execution": False,
        "model_prompts_sent": False,
        "enqueue_requested": True,
        "enqueue_performed": bool(enqueue.get("enqueue_performed")),
        "bridge": bridged,
        "enqueue": enqueue,
    }


def _run_controller_command(repo_root: Path, command: str, args: list[str]) -> tuple[int, str, str]:
    """Run the durable controller without exposing a shell boundary."""
    script = repo_script("continuity_controller.py", repo_root)
    proc = subprocess.run(
        [sys.executable, str(script), command, *args],
        check=False,
        cwd=str(script.parent),
        env=_execution_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=86400,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _run_sqlite_command(repo_root: Path, command: str, args: list[str]) -> tuple[int, str, str]:
    """Run the SQLite controller with the same execution environment."""
    script = repo_script("sqlite_controller.py", repo_root)
    proc = subprocess.run(
        [sys.executable, str(script), command, *args],
        check=False,
        cwd=str(script.parent),
        env=_execution_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=86400,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _sqlite_db_path(args: argparse.Namespace, *, packet: dict[str, Any] | None = None) -> Path:
    """Resolve the durable SQLite path used by the ``auto`` backend.

    The path is deliberately deterministic and local to the run/workspace so
    a detached controller can be reattached after the originating chat exits.
    An explicit ``--db`` or ``LAD_DB_PATH`` always wins.  Packet workspace is
    used for enqueue when no run directory was supplied, keeping enqueue and
    run naturally paired for a freshly created task.
    """
    explicit = getattr(args, "db", None)
    if explicit:
        return Path(str(explicit)).expanduser().resolve()
    configured = os.environ.get("LAD_DB_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    run_dir = getattr(args, "run_dir", None)
    if run_dir:
        return Path(str(run_dir)).expanduser().resolve() / "dispatch.sqlite3"
    workspace = getattr(args, "workspace", None)
    if workspace:
        root = Path(str(workspace)).expanduser().resolve()
    elif packet and packet.get("workspace"):
        root = Path(str(packet["workspace"])).expanduser().resolve()
    else:
        root = Path.cwd().resolve()
    return root / ".lad" / "dispatch.sqlite3"


def _select_backend(
    args: argparse.Namespace,
    operation: str,
    *,
    packet: dict[str, Any] | None = None,
) -> tuple[str, Path | None, str]:
    """Select a controller backend without silently converting legacy runs.

    ``auto`` is SQLite-first for new work.  An existing JSON ``state.json``
    is an explicit migration signal, so auto continues that run with the JSON
    controller instead of creating a second queue.  Users can always force a
    backend with ``--backend json`` or ``--backend sqlite``.
    """
    requested = str(getattr(args, "backend", "auto") or "auto").lower()
    if requested not in {"auto", "json", "sqlite"}:
        raise ValueError(f"unsupported backend: {requested}")
    db_path = _sqlite_db_path(args, packet=packet)
    if requested == "sqlite":
        if not getattr(args, "db", None) and not os.environ.get("LAD_DB_PATH"):
            # Explicit SQLite remains strict: callers should see exactly where
            # durable state will live rather than accidentally guessing.
            raise ValueError(f"{operation}: --db is required with --backend sqlite")
        return "sqlite", db_path, "explicit_sqlite"
    if requested == "json":
        return "json", None, "explicit_json_legacy"

    run_dir_value = getattr(args, "run_dir", None)
    run_dir = Path(str(run_dir_value)).expanduser().resolve() if run_dir_value else None
    if run_dir and (run_dir / "state.json").is_file() and not db_path.exists():
        return "json", None, "existing_json_state"
    reason = "existing_sqlite_db" if db_path.is_file() else "new_sqlite_default"
    return "sqlite", db_path, reason


def _not_initialized_payload(
    command: str,
    args: argparse.Namespace,
    backend_reason: str,
    db_path: Path,
) -> dict[str, Any]:
    """Return a read-only status for an auto backend with no durable DB.

    ``status`` and ``resume`` are queries/reconciliation boundaries.  They
    must not create a new SQLite schema merely because a user asks whether a
    queue exists; enqueue/run remain the explicit initialization operations.
    """
    return {
        "command": command,
        "schema_version": 1,
        "ok": True,
        "backend": "sqlite",
        "status": "not_initialized",
        "initialized": False,
        "backend_requested": str(args.backend),
        "backend_resolution": {
            "selected": "sqlite",
            "reason": backend_reason,
            "db_path": str(db_path),
        },
    }


def _parse_object(stdout: str, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} must return a JSON object")
    return payload


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write controller metadata without making it world-readable."""
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(path.parent),
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = handle.name
            os.chmod(handle.name, 0o600)
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _detached_controller_spec(repo_root: Path, args: argparse.Namespace) -> tuple[list[str], Path, Path]:
    """Build a no-shell controller command plus its log and PID metadata paths."""
    backend, db_path, _reason = _select_backend(args, "run")
    workspace = Path(args.workspace).expanduser().resolve()
    if backend == "sqlite":
        if db_path is None:  # pragma: no cover - defensive; selector always supplies it
            raise ValueError("run: SQLite backend did not resolve a database path")
        db = db_path
        script = repo_script("sqlite_controller.py", repo_root)
        command = [
            sys.executable, str(script), "run", "--db", str(db),
            "--workspace", str(workspace),
            "--poll-seconds", str(max(1, int(args.poll_seconds))),
            "--max-idle-rounds", str(max(0, int(args.max_idle_rounds or 0))),
            "--max-lanes", str(max(1, int(args.max_lanes))),
        ]
        if args.once:
            command.append("--once")
        if args.inventory:
            command.extend(["--inventory", str(Path(args.inventory).expanduser().resolve())])
        if args.runtime_state:
            command.extend(["--runtime-state", str(Path(args.runtime_state).expanduser().resolve())])
        if args.owner_id:
            command.extend(["--owner-id", str(args.owner_id)])
        default_base = db.with_name(db.name + ".controller")
    else:
        if not args.run_dir:
            raise ValueError("run: --run-dir is required with --backend json")
        if int(args.max_lanes) > 1:
            raise ValueError("run: --max-lanes > 1 requires --backend sqlite")
        run_dir = Path(args.run_dir).expanduser().resolve()
        script = repo_script("continuity_controller.py", repo_root)
        command = [
            sys.executable, str(script), "run", "--run-dir", str(run_dir),
            "--poll-seconds", str(max(1, int(args.poll_seconds))),
            "--max-idle-rounds", str(max(0, int(args.max_idle_rounds or 0))),
        ]
        if args.once:
            command.append("--once")
        if args.owner_id:
            command.extend(["--owner-id", str(args.owner_id)])
        default_base = run_dir / "controller"
    log_path = Path(args.log).expanduser().resolve() if args.log else default_base.with_suffix(".log")
    pid_path = Path(args.pid_file).expanduser().resolve() if args.pid_file else default_base.with_suffix(".pid")
    return command, log_path, pid_path


def _start_detached_controller(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = locate_repo_root()
    command, log_path, pid_path = _detached_controller_spec(repo_root, args)
    if pid_path.exists():
        try:
            previous = json.loads(pid_path.read_text(encoding="utf-8"))
            previous_pid = int(previous.get("pid") or 0)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            previous_pid = 0
        if _pid_is_alive(previous_pid):
            raise RuntimeError(f"controller already running with pid {previous_pid}")
        # A stale metadata file is safe to replace; it never identifies a
        # live process at this point.
        pid_path.unlink(missing_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        os.chmod(log_path, 0o600)
        process = subprocess.Popen(
            command,
            cwd=str(repo_root),
            env=_execution_environment(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    metadata = {
        "schema_version": 1,
        "ok": True,
        "detached": True,
        "backend": str(args.backend),
        "backend_effective": (
            "sqlite"
            if any(Path(str(item)).name == "sqlite_controller.py" for item in command)
            else "json"
        ),
        "pid": int(process.pid),
        "started_at_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "log_path": str(log_path),
        "pid_path": str(pid_path),
        "command": command,
        "chat_independent": True,
        "provider_prompt_sent_by_start": False,
    }
    try:
        _write_private_json(pid_path, metadata)
    except Exception:
        try:
            process.terminate()
        except OSError:
            pass
        raise
    return metadata


def _controller_status(repo_root: Path, run_dir: Path) -> dict[str, Any]:
    code, stdout, stderr = _run_controller_command(
        repo_root, "status", ["--run-dir", str(run_dir.expanduser().resolve())]
    )
    if code != 0:
        raise RuntimeError(stderr or stdout or f"controller status exited {code}")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"controller status returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("controller status must return an object")
    return payload


def _run_plan(state: Path, jobs: Path, max_lanes: int, horizon: int) -> tuple[int, dict[str, Any]]:
    repo_root = locate_repo_root()
    script = repo_script("dynamic_dispatch_planner.py", repo_root)
    for path, label in ((state, "state"), (jobs, "jobs")):
        if not path.is_file():
            raise FileNotFoundError(f"{label} file does not exist: {path}")
    return _run_json_script(
        script,
        [
            "--state", str(state.expanduser().resolve()),
            "--jobs", str(jobs.expanduser().resolve()),
            "--max-lanes", str(max(1, max_lanes)),
            "--horizon", str(max(1, horizon)),
        ],
    )


def _run_monitor(
    state: Path,
    duration_seconds: float,
    interval_seconds: float,
    stall_seconds: float,
    refresh_codex: bool,
    refresh_compute: bool,
) -> tuple[int, dict[str, Any]]:
    repo_root = locate_repo_root()
    script = repo_script("dispatch_monitor.py", repo_root)
    if not state.is_file():
        raise FileNotFoundError(f"state file does not exist: {state}")
    args = [
        "--state", str(state.expanduser().resolve()),
        "--duration-seconds", str(max(0.0, duration_seconds)),
        "--interval-seconds", str(max(0.1, interval_seconds)),
        "--stall-seconds", str(max(1.0, stall_seconds)),
    ]
    args.append("--refresh-codex-usage" if refresh_codex else "--no-refresh-codex-usage")
    args.append("--refresh-compute-hosts" if refresh_compute else "--no-refresh-compute-hosts")
    return _run_json_script(
        script,
        args,
        timeout_seconds=max(20.0, float(duration_seconds) + max(30.0, float(interval_seconds) * 2.0)),
    )


def _run_monitor_state(db: Path | None, snapshot: Path | None) -> tuple[int, dict[str, Any]]:
    """Project durable controller state into the monitor worker contract."""
    if (db is None) == (snapshot is None):
        raise ValueError("monitor-state requires exactly one of --db or --snapshot")
    script = repo_script("controller_monitor_adapter.py", locate_repo_root())
    args = ["--db", str(db.expanduser().resolve())] if db is not None else [
        "--snapshot", str(snapshot.expanduser().resolve())
    ]
    source = db or snapshot
    if source is None or not source.is_file():
        raise FileNotFoundError(f"monitor-state source does not exist: {source}")
    return _run_json_script(script, args, timeout_seconds=20.0)


def _run_replan(
    monitor_report: Path,
    jobs: Path | None,
    plan: Path | None,
    state: Path | None = None,
    run_planner: bool = False,
    max_lanes: int = 4,
    horizon: int = 8,
    apply: bool = False,
    merged_state_out: Path | None = None,
    merged_jobs_out: Path | None = None,
    next_plan_out: Path | None = None,
) -> tuple[int, dict[str, Any]]:
    repo_root = locate_repo_root()
    script = repo_script("replan_controller.py", repo_root)
    if not monitor_report.is_file():
        raise FileNotFoundError(f"monitor report does not exist: {monitor_report}")
    args = ["--monitor-report", str(monitor_report.expanduser().resolve())]
    if jobs is not None:
        if not jobs.is_file():
            raise FileNotFoundError(f"jobs file does not exist: {jobs}")
        args.extend(["--jobs", str(jobs.expanduser().resolve())])
    if plan is not None:
        if not plan.is_file():
            raise FileNotFoundError(f"plan file does not exist: {plan}")
        args.extend(["--plan", str(plan.expanduser().resolve())])
    if state is not None:
        if not state.is_file():
            raise FileNotFoundError(f"state file does not exist: {state}")
        args.extend(["--state", str(state.expanduser().resolve())])
    if run_planner:
        args.extend(["--run-planner", "--max-lanes", str(max(1, max_lanes)), "--horizon", str(max(1, horizon))])
    if apply:
        if merged_state_out is None or merged_jobs_out is None:
            raise ValueError("--apply requires --merged-state-out and --merged-jobs-out")
        args.extend([
            "--apply",
            "--merged-state-out", str(merged_state_out.expanduser().resolve()),
            "--merged-jobs-out", str(merged_jobs_out.expanduser().resolve()),
        ])
    elif merged_state_out is not None or merged_jobs_out is not None:
        raise ValueError("merged output paths require --apply")
    if next_plan_out is not None:
        if not run_planner:
            raise ValueError("--next-plan-out requires --run-planner")
        args.extend(["--next-plan-out", str(next_plan_out.expanduser().resolve())])
    return _run_json_script(script, args)


def _run_closed_loop(
    approved_packets: Path,
    *,
    workspace: Path,
    approved: bool,
    fake_execute: bool,
    db: Path | None,
    max_lanes: int,
    monitor_duration_seconds: float,
    monitor_interval_seconds: float,
    monitor_stall_seconds: float,
    jobs: Path | None,
    state: Path | None,
    plan: Path | None,
    output: Path | None,
) -> tuple[int, dict[str, Any]]:
    """Run the provider-free approved-wave control loop.

    This wrapper deliberately uses the scrubbed environment.  The closed-loop
    script has only dry-run and fake-execute modes, so no provider credentials
    are needed or forwarded by the packaged command.
    """

    repo_root = locate_repo_root()
    script = repo_script("dispatch_closed_loop.py", repo_root)
    if not approved_packets.expanduser().is_file() and str(approved_packets) != "-":
        raise FileNotFoundError(f"approved packet bundle does not exist: {approved_packets}")
    workspace = workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise NotADirectoryError(f"workspace does not exist: {workspace}")
    args = [
        "--approved-packets", str(approved_packets.expanduser().resolve())
        if str(approved_packets) != "-" else "-",
        "--workspace", str(workspace),
        "--mode", "fake-execute" if fake_execute else "dry-run",
        "--max-lanes", str(max(1, int(max_lanes))),
        "--monitor-duration-seconds", str(max(0.0, float(monitor_duration_seconds))),
        "--monitor-interval-seconds", str(max(0.1, float(monitor_interval_seconds))),
        "--monitor-stall-seconds", str(max(1.0, float(monitor_stall_seconds))),
    ]
    if approved:
        args.append("--approved")
    if fake_execute:
        if db is None:
            raise ValueError("closed-loop --fake-execute requires --db")
        args.extend(["--db", str(db.expanduser().resolve())])
    if jobs is not None:
        if not jobs.expanduser().is_file():
            raise FileNotFoundError(f"jobs file does not exist: {jobs}")
        args.extend(["--jobs", str(jobs.expanduser().resolve())])
    if state is not None:
        if not state.expanduser().is_file():
            raise FileNotFoundError(f"planner state does not exist: {state}")
        args.extend(["--state", str(state.expanduser().resolve())])
    if plan is not None:
        if not plan.expanduser().is_file():
            raise FileNotFoundError(f"dispatch plan does not exist: {plan}")
        args.extend(["--plan", str(plan.expanduser().resolve())])
    if output is not None:
        args.extend(["--output", str(output.expanduser().resolve())])

    proc = subprocess.run(
        [sys.executable, str(script), *args],
        check=False,
        cwd=str(workspace),
        env=_safe_scan_environment(),
        stdin=None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=max(
            30.0,
            float(monitor_duration_seconds)
            + max(30.0, float(monitor_interval_seconds) * 2.0),
        ),
    )
    stdout = proc.stdout.strip()
    if not stdout and output is not None and output.expanduser().is_file():
        stdout = output.expanduser().read_text(encoding="utf-8").strip()
    if not stdout:
        return proc.returncode, {
            "ok": False,
            "error": "closed-loop produced no JSON output",
            "stderr": proc.stderr.strip(),
        }
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return proc.returncode, {
            "ok": False,
            "error": f"closed-loop output is not valid JSON: {exc}",
            "stderr": proc.stderr.strip(),
        }
    if not isinstance(payload, dict):
        payload = {"value": payload}
    payload.setdefault("ok", proc.returncode == 0)
    if proc.stderr.strip():
        payload.setdefault("stderr", proc.stderr.strip()[-2000:])
    return proc.returncode, payload


def _command_version() -> int:
    print(__version__)
    return 0


def _command_doctor(args: argparse.Namespace) -> int:
    if not args.offline:
        print("doctor: --offline is required", file=sys.stderr)
        return 2

    try:
        payload = _run_scan_command(Path.cwd(), args.expect_provider)
    except (FileNotFoundError, NotADirectoryError, RuntimeError) as exc:
        print(f"doctor: {exc}", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired as exc:
        print(f"doctor: scan timed out: {exc}", file=sys.stderr)
        return 2
    provider_check = payload["provider_check"]
    if not provider_check["ok"]:
        print(
            "doctor: offline provider check failed: "
            f"{', '.join(provider_check['missing'])}",
            file=sys.stderr,
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 2
    doctor_report = {
        "command": "doctor",
        "mode": "offline",
        "version": __version__,
        "evidence_level": "local-only",
        "evidence": {
            "provider_contacts": "none",
            "model_prompt_sent": False,
            "network_probes": False,
        },
        **payload,
    }
    print(json.dumps(doctor_report, indent=2, sort_keys=True))
    return 0


def _command_demo(args: argparse.Namespace) -> int:
    if not args.offline:
        print("demo: --offline is required for this lane", file=sys.stderr)
        return 2

    try:
        payload = _run_scan_command(Path.cwd(), args.expect_provider)
    except (FileNotFoundError, NotADirectoryError, RuntimeError) as exc:
        print(f"demo: {exc}", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired as exc:
        print(f"demo: scan timed out: {exc}", file=sys.stderr)
        return 2

    demo_payload = {
        "command": "demo",
        "mode": "offline",
        "version": __version__,
        "evidence_level": "local-only",
        "message": (
            "Offline demo uses local_system_scan and repo script discovery only. "
            "No provider contact and no model prompt was sent."
        ),
        "commands": [
            "lad --version",
            "lad doctor --offline",
            "lad scan --workspace <PATH>",
        ],
        **payload,
    }
    print(json.dumps(demo_payload, indent=2, sort_keys=True))
    return 0


def _command_scan(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace)
    if not workspace.exists():
        print(f"scan: workspace does not exist: {workspace}", file=sys.stderr)
        return 2
    if not workspace.is_dir():
        print(f"scan: workspace is not a directory: {workspace}", file=sys.stderr)
        return 2

    try:
        payload = _run_scan_command(workspace, args.expect_provider)
    except (FileNotFoundError, NotADirectoryError, RuntimeError) as exc:
        print(f"scan: {exc}", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired as exc:
        print(f"scan: timed out: {exc}", file=sys.stderr)
        return 2

    if args.expect_provider and not payload["provider_check"]["ok"]:
        print(
            "scan: requested providers are not available: "
            f"{', '.join(payload['provider_check']['missing'])}",
            file=sys.stderr,
        )
        print(json.dumps({"command": "scan", "workspace": str(workspace.resolve()), **payload}, indent=2, sort_keys=True))
        return 2

    if not isinstance(payload.get("scan"), dict) or not payload["scan"].get("ok"):
        print(f"scan: local scan reported failure for {workspace}", file=sys.stderr)
        print(json.dumps({"command": "scan", "workspace": str(workspace.resolve()), **payload}, indent=2, sort_keys=True))
        return 2

    print(
        json.dumps(
            {
                "command": "scan",
                "workspace": str(workspace.resolve()),
                "offline": True,
                **payload,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _command_governor(args: argparse.Namespace) -> int:
    """Report live local memory admission without mutating processes."""
    try:
        script = repo_script("resource_governor.py", locate_repo_root())
        command_args = [
            "--requested-lanes", str(max(0, int(args.requested_lanes))),
            "--per-lane-peak-mib", str(max(0, int(args.per_lane_peak_mib))),
            "--max-local-lanes", str(max(0, int(args.max_local_lanes))),
        ]
        for pid in args.owned_pid:
            command_args.extend(["--owned-pid", str(int(pid))])
        _, payload = _run_json_script(script, command_args, timeout_seconds=args.timeout)
    except (FileNotFoundError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"governor: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"command": "governor", **payload}, indent=2, sort_keys=True))
    return 0 if payload.get("ok", True) is not False else 2


def _command_legacy_import(args: argparse.Namespace) -> int:
    """Audit legacy JSON runs, optionally writing a new SQLite copy."""
    try:
        script = repo_script("legacy_history.py", locate_repo_root())
        command_args = ["--root", str(Path(args.root).expanduser().resolve()), "--max-runs", str(max(1, int(args.max_runs)))]
        if args.reconcile:
            command_args.append("--reconcile")
        if args.output_db:
            command_args.extend(["--output-db", str(Path(args.output_db).expanduser().resolve())])
        _, payload = _run_json_script(script, command_args, timeout_seconds=args.timeout)
    except (FileNotFoundError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"legacy-import: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"command": "legacy-import", **payload}, indent=2, sort_keys=True))
    return 0 if payload.get("ok", True) is not False else 2


def _command_cockpit(args: argparse.Namespace) -> int:
    """Render the compact read-only L0 Mission Cockpit."""
    try:
        script = repo_script("mission_cockpit.py", locate_repo_root())
        command_args = ["--snapshot", str(Path(args.snapshot).expanduser().resolve())]
        for flag, value in (("--mission", args.mission), ("--governor", args.governor), ("--history", args.history)):
            if value:
                command_args.extend([flag, str(Path(value).expanduser().resolve())])
        _, payload = _run_json_script(script, command_args, timeout_seconds=args.timeout)
    except (FileNotFoundError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"cockpit: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"command": "cockpit", **payload}, indent=2, sort_keys=True))
    return 0 if payload.get("ok", True) is not False else 2


def _command_fit(args: argparse.Namespace) -> int:
    try:
        payload = _run_hardware_fit(
            Path(args.preflight), Path(args.jobs), Path(args.workspace)
        )
    except (FileNotFoundError, NotADirectoryError, RuntimeError, ValueError) as exc:
        print(f"fit: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"command": "fit", "read_only": True, **payload}, indent=2, sort_keys=True))
    return 0


def _command_estimate(args: argparse.Namespace) -> int:
    try:
        returncode, payload = _run_task_estimate(
            args.task,
            Path(args.repo_root) if args.repo_root else None,
            Path(args.manifest) if args.manifest else None,
            Path(args.history) if args.history else None,
        )
    except (FileNotFoundError, NotADirectoryError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"estimate: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"command": "estimate", "read_only": True, **payload}, indent=2, sort_keys=True))
    return int(returncode if not payload.get("ok", True) else 0)


def _command_capture(args: argparse.Namespace) -> int:
    try:
        returncode, payload = _run_task_capture(
            args.task,
            Path(args.repo_root) if args.repo_root else None,
            Path(args.manifest) if args.manifest else None,
            Path(args.git_metadata) if args.git_metadata else None,
            Path(args.policy) if args.policy else None,
            Path(args.history) if args.history else None,
            args.model,
            args.host,
        )
    except (FileNotFoundError, NotADirectoryError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"capture: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"command": "capture", **payload}, indent=2, sort_keys=True))
    return int(returncode if not payload.get("ok", True) else 0)


def _command_evidence(args: argparse.Namespace) -> int:
    """Build a search-first compatibility plan without contacting a provider."""
    from .discovery import build_search_plan, resolve_capability

    plan = build_search_plan(
        provider=args.provider,
        capability=args.capability,
        version=args.provider_version,
        model=args.model,
        host=args.host,
        official_domains=tuple(args.official_domain) if args.official_domain else None,
    )
    payload: dict[str, Any] = {
        "command": "evidence",
        "read_only": True,
        "network_contacted": False,
        "model_prompt_sent": False,
        "search_plan": plan,
    }
    if args.sources:
        source_path = Path(args.sources).expanduser().resolve()
        if not source_path.is_file():
            print(f"evidence: sources file does not exist: {source_path}", file=sys.stderr)
            return 2
        try:
            source_payload = json.loads(source_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"evidence: invalid sources file: {exc}", file=sys.stderr)
            return 2
        records = source_payload.get("records") if isinstance(source_payload, dict) else source_payload
        if not isinstance(records, list):
            print("evidence: sources must be a JSON list or an object with records", file=sys.stderr)
            return 2
        payload["resolution"] = resolve_capability(
            {
                "provider": args.provider,
                "capability": args.capability,
                "version": args.provider_version,
                "model": args.model,
                "host": args.host,
            },
            records,
            now_utc=args.now,
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _command_dispatch(args: argparse.Namespace) -> int:
    try:
        returncode, payload = _run_dispatch_workflow(
            Path(args.workspace),
            Path(args.jobs),
            Path(args.preflight) if args.preflight else None,
            Path(args.inventory) if args.inventory else None,
            Path(args.model_state) if args.model_state else None,
            Path(args.runtime_state) if args.runtime_state else None,
            Path(args.manifest) if args.manifest else None,
            Path(args.history) if args.history else None,
            Path(args.output) if args.output else None,
            args.timeout,
            args.max_lanes,
            args.horizon,
            args.live_probes,
            args.skip_antigravity_usage,
        )
    except (FileNotFoundError, NotADirectoryError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"dispatch: {exc}", file=sys.stderr)
        return 2
    if args.enqueue:
        if not args.adapters or not args.db:
            print("dispatch: --enqueue/--execute requires --adapters and --db", file=sys.stderr)
            return 2
        try:
            enqueue_report = _enqueue_dispatch_report(
                payload,
                jobs=Path(args.jobs).expanduser().resolve(),
                workspace=Path(args.workspace).expanduser().resolve(),
                preflight=Path(args.preflight).expanduser().resolve() if args.preflight else None,
                adapters=Path(args.adapters).expanduser().resolve(),
                db=Path(args.db).expanduser().resolve(),
            )
        except (FileNotFoundError, NotADirectoryError, RuntimeError, ValueError, OSError) as exc:
            print(f"dispatch enqueue: {exc}", file=sys.stderr)
            return 2
        payload = dict(payload)
        payload["read_only"] = False
        payload["enqueue_requested"] = True
        payload["enqueue_performed"] = bool(enqueue_report.get("enqueue_performed"))
        payload["enqueue"] = enqueue_report
        payload["ok"] = bool(payload.get("ok") and enqueue_report.get("ok"))
        if not enqueue_report.get("ok"):
            returncode = max(int(returncode), 2)
        # Keep an explicitly requested report path auditable: the persisted
        # report reflects the enqueue decision, while the default path remains
        # the original read-only workflow artifact.
        if args.output:
            output_path = Path(args.output).expanduser().resolve()
            output_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    print(json.dumps({"command": "dispatch", **payload}, indent=2, sort_keys=True))
    return int(returncode if not payload.get("ok") else 0)


def _command_preflight(args: argparse.Namespace) -> int:
    try:
        returncode, payload = _run_preflight(
            Path(args.workspace),
            Path(args.inventory),
            Path(args.output) if args.output else None,
            Path(args.model_state) if args.model_state else None,
            Path(args.runtime_state) if args.runtime_state else None,
            args.timeout,
            args.skip_antigravity_usage,
        )
    except (FileNotFoundError, NotADirectoryError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"preflight: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"command": "preflight", **payload}, indent=2, sort_keys=True))
    return int(returncode if not payload.get("ok") else 0)


def _command_bridge(args: argparse.Namespace) -> int:
    try:
        returncode, payload = _run_plan_bridge(
            Path(args.plan),
            Path(args.jobs),
            Path(args.state),
            Path(args.adapters),
            args.mode,
            enqueue=bool(args.enqueue),
            db=Path(args.db) if args.db else None,
        )
    except (FileNotFoundError, NotADirectoryError, RuntimeError, ValueError) as exc:
        print(f"bridge: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"command": "bridge", **payload}, indent=2, sort_keys=True))
    return int(returncode if not payload.get("ok") else 0)


def _command_plan(args: argparse.Namespace) -> int:
    try:
        returncode, payload = _run_plan(
            Path(args.state), Path(args.jobs), args.max_lanes, args.horizon
        )
    except (FileNotFoundError, NotADirectoryError, RuntimeError, ValueError) as exc:
        print(f"plan: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"command": "plan", **payload}, indent=2, sort_keys=True))
    return int(returncode if not payload.get("ok") else 0)


def _command_run(args: argparse.Namespace) -> int:
    repo_root = locate_repo_root()
    if args.detach:
        try:
            payload = _start_detached_controller(args)
        except (FileNotFoundError, NotADirectoryError, RuntimeError, ValueError, OSError) as exc:
            print(f"run: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({"command": "run", **payload}, indent=2, sort_keys=True))
        return 0
    try:
        backend, db_path, backend_reason = _select_backend(args, "run")
    except ValueError as exc:
        print(f"run: {exc}", file=sys.stderr)
        return 2
    if backend == "sqlite":
        if db_path is None:  # pragma: no cover - defensive
            print("run: SQLite backend did not resolve a database path", file=sys.stderr)
            return 2
        sqlite_args = ["--db", str(db_path)]
        sqlite_args.extend(["--workspace", str(Path(args.workspace).expanduser().resolve())])
        if args.inventory:
            sqlite_args.extend(["--inventory", str(Path(args.inventory).expanduser().resolve())])
        if args.runtime_state:
            sqlite_args.extend(["--runtime-state", str(Path(args.runtime_state).expanduser().resolve())])
        if args.once:
            sqlite_args.append("--once")
        sqlite_args.extend(["--poll-seconds", str(max(0.1, args.poll_seconds))])
        sqlite_args.extend(["--max-idle-rounds", str(max(0, int(args.max_idle_rounds or 0)))])
        sqlite_args.extend(["--max-lanes", str(max(1, args.max_lanes))])
        if args.owner_id:
            sqlite_args.extend(["--owner-id", str(args.owner_id)])
        try:
            returncode, stdout, stderr = _run_sqlite_command(repo_root, "run", sqlite_args)
            payload = _parse_object(stdout, "sqlite controller")
        except (FileNotFoundError, NotADirectoryError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            print(f"run: {exc}", file=sys.stderr)
            return 2
        payload = {
            "command": "run", **payload, "controller_stderr": stderr,
            "backend_requested": str(args.backend),
            "backend_resolution": {"selected": backend, "reason": backend_reason, "db_path": str(db_path)},
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return int(returncode)
    if not args.run_dir:
        print("run: --run-dir is required with --backend json", file=sys.stderr)
        return 2
    if args.max_lanes > 1:
        print("run: --max-lanes > 1 requires --backend sqlite", file=sys.stderr)
        return 2
    run_dir = Path(args.run_dir).expanduser().resolve()
    controller_args = ["--run-dir", str(run_dir)]
    if args.once:
        controller_args.append("--once")
    controller_args.extend(["--poll-seconds", str(max(1, args.poll_seconds))])
    if args.max_idle_rounds:
        controller_args.extend(["--max-idle-rounds", str(max(0, int(args.max_idle_rounds or 0)))])
    if args.owner_id:
        controller_args.extend(["--owner-id", str(args.owner_id)])
    try:
        returncode, stdout, stderr = _run_controller_command(repo_root, "run", controller_args)
        status = _controller_status(repo_root, run_dir)
    except (FileNotFoundError, NotADirectoryError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"run: {exc}", file=sys.stderr)
        return 2
    payload = {
        "command": "run",
        "backend_requested": str(args.backend),
        "backend_resolution": {"selected": backend, "reason": backend_reason},
        "controller_returncode": returncode,
        "controller_stdout": stdout,
        "controller_stderr": stderr,
        "status": status,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return int(returncode)


def _command_resume(args: argparse.Namespace) -> int:
    repo_root = locate_repo_root()
    try:
        backend, db_path, backend_reason = _select_backend(args, "resume")
    except ValueError as exc:
        print(f"resume: {exc}", file=sys.stderr)
        return 2
    if backend == "sqlite":
        if db_path is None:  # pragma: no cover - defensive
            print("resume: SQLite backend did not resolve a database path", file=sys.stderr)
            return 2
        if str(args.backend) == "auto" and not db_path.is_file():
            print(
                json.dumps(
                    _not_initialized_payload("resume", args, backend_reason, db_path),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        try:
            returncode, stdout, stderr = _run_sqlite_command(
                repo_root, "resume", ["--db", str(db_path)]
            )
            payload = _parse_object(stdout, "sqlite controller")
        except (FileNotFoundError, NotADirectoryError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            print(f"resume: {exc}", file=sys.stderr)
            return 2
        payload = {
            "command": "resume", **payload, "controller_stderr": stderr,
            "backend_requested": str(args.backend),
            "backend_resolution": {"selected": backend, "reason": backend_reason, "db_path": str(db_path)},
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return int(returncode)
    if not args.run_dir:
        print("resume: --run-dir is required with --backend json", file=sys.stderr)
        return 2
    run_dir = Path(args.run_dir).expanduser().resolve()
    try:
        returncode, stdout, stderr = _run_controller_command(
            repo_root, "resume", ["--run-dir", str(run_dir)]
        )
        status = _controller_status(repo_root, run_dir)
    except (FileNotFoundError, NotADirectoryError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"resume: {exc}", file=sys.stderr)
        return 2
    payload = {
        "command": "resume",
        "backend_requested": str(args.backend),
        "backend_resolution": {"selected": backend, "reason": backend_reason},
        "controller_returncode": returncode,
        "controller_stdout": stdout,
        "controller_stderr": stderr,
        "status": status,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return int(returncode)


def _command_monitor(args: argparse.Namespace) -> int:
    try:
        returncode, payload = _run_monitor(
            Path(args.state), args.duration_seconds, args.interval_seconds,
            args.stall_seconds, args.refresh_codex_usage, args.refresh_compute_hosts,
        )
    except (FileNotFoundError, NotADirectoryError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"monitor: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"command": "monitor", **payload}, indent=2, sort_keys=True))
    return int(returncode if not payload.get("ok") else 0)


def _command_monitor_state(args: argparse.Namespace) -> int:
    try:
        returncode, payload = _run_monitor_state(
            Path(args.db) if args.db else None,
            Path(args.snapshot) if args.snapshot else None,
        )
    except (FileNotFoundError, NotADirectoryError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"monitor-state: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"command": "monitor-state", **payload}, indent=2, sort_keys=True))
    return int(returncode if not payload.get("ok", True) else 0)


def _command_replan(args: argparse.Namespace) -> int:
    try:
        returncode, payload = _run_replan(
            Path(args.monitor_report),
            Path(args.jobs) if args.jobs else None,
            Path(args.plan) if args.plan else None,
            Path(args.state) if args.state else None,
            args.run_planner,
            args.max_lanes,
            args.horizon,
            args.apply,
            Path(args.merged_state_out) if args.merged_state_out else None,
            Path(args.merged_jobs_out) if args.merged_jobs_out else None,
            Path(args.next_plan_out) if args.next_plan_out else None,
        )
    except (FileNotFoundError, NotADirectoryError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"replan: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"command": "replan", **payload}, indent=2, sort_keys=True))
    return int(returncode if not payload.get("ok") else 0)


def _command_closed_loop(args: argparse.Namespace) -> int:
    """Consume an explicitly approved packet wave and observe/replan it."""

    try:
        returncode, payload = _run_closed_loop(
            Path(args.approved_packets),
            workspace=Path(args.workspace),
            approved=bool(args.approved),
            fake_execute=bool(args.fake_execute),
            db=Path(args.db) if args.db else None,
            max_lanes=args.max_lanes,
            monitor_duration_seconds=args.monitor_duration_seconds,
            monitor_interval_seconds=args.monitor_interval_seconds,
            monitor_stall_seconds=args.monitor_stall_seconds,
            jobs=Path(args.jobs) if args.jobs else None,
            state=Path(args.state) if args.state else None,
            plan=Path(args.plan) if args.plan else None,
            output=Path(args.output) if args.output else None,
        )
    except (FileNotFoundError, NotADirectoryError, RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"closed-loop: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"command": "closed-loop", **payload}, indent=2, sort_keys=True))
    return int(returncode if not payload.get("ok") else 0)


def _command_enqueue(args: argparse.Namespace) -> int:
    repo_root = locate_repo_root()
    if not Path(args.job_file).expanduser().is_file():
        print(f"enqueue: job file does not exist: {args.job_file}", file=sys.stderr)
        return 2
    try:
        packet_hint: dict[str, Any] | None = None
        if str(args.backend) == "auto" and not args.db and not args.run_dir:
            try:
                candidate = json.loads(Path(args.job_file).expanduser().read_text(encoding="utf-8"))
                if isinstance(candidate, dict):
                    packet_hint = candidate
            except (OSError, ValueError, json.JSONDecodeError):
                # The selected controller will emit the authoritative packet
                # validation error; path selection must remain side-effect free.
                packet_hint = None
        backend, db_path, backend_reason = _select_backend(args, "enqueue", packet=packet_hint)
        if backend == "sqlite":
            if db_path is None:  # pragma: no cover - defensive
                raise ValueError("SQLite backend did not resolve a database path")
            code, stdout, stderr = _run_sqlite_command(
                repo_root,
                "enqueue",
                ["--db", str(db_path), "--job-file", str(Path(args.job_file).expanduser().resolve())],
            )
        else:
            if not args.run_dir:
                raise ValueError("--run-dir is required with --backend json")
            code, stdout, stderr = _run_controller_command(
                repo_root,
                "enqueue",
                ["--run-dir", str(Path(args.run_dir).expanduser().resolve()), "--job-file", str(Path(args.job_file).expanduser().resolve())],
            )
        payload = _parse_object(stdout, "controller") if stdout else {"ok": code == 0}
    except (FileNotFoundError, NotADirectoryError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"enqueue: {exc}", file=sys.stderr)
        return 2
    payload = {
        "command": "enqueue", **payload, "stderr": stderr,
        "backend_requested": str(args.backend),
        "backend_resolution": {
            "selected": backend,
            "reason": backend_reason,
            **({"db_path": str(db_path)} if db_path is not None else {}),
        },
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return int(code)


def _command_status(args: argparse.Namespace) -> int:
    repo_root = locate_repo_root()
    try:
        backend, db_path, backend_reason = _select_backend(args, "status")
        if backend == "sqlite":
            if db_path is None:  # pragma: no cover - defensive
                raise ValueError("SQLite backend did not resolve a database path")
            if str(args.backend) == "auto" and not db_path.is_file():
                print(
                    json.dumps(
                        _not_initialized_payload("status", args, backend_reason, db_path),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            code, stdout, stderr = _run_sqlite_command(
                repo_root, "status", ["--db", str(db_path)]
            )
        else:
            if not args.run_dir:
                raise ValueError("--run-dir is required with --backend json")
            code, stdout, stderr = _run_controller_command(
                repo_root, "status", ["--run-dir", str(Path(args.run_dir).expanduser().resolve())]
            )
        payload = _parse_object(stdout, "controller status")
    except (FileNotFoundError, NotADirectoryError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"status: {exc}", file=sys.stderr)
        return 2
    payload = {
        "command": "status", **payload, "stderr": stderr,
        "backend_requested": str(args.backend),
        "backend_resolution": {
            "selected": backend,
            "reason": backend_reason,
            **({"db_path": str(db_path)} if db_path is not None else {}),
        },
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return int(code)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lad", description="Local agent dispatch thin entry point")
    parser.add_argument("--version", action="store_true", help="show tool version")

    subparsers = parser.add_subparsers(dest="command")
    doctor = subparsers.add_parser("doctor", help="run local evidence scan without network")
    doctor.add_argument("--offline", action="store_true", help="run offline-only checks")
    doctor.add_argument(
        "--expect-provider",
        action="append",
        default=[],
        help="fail if the provider dependency is absent",
    )
    doctor.set_defaults(func=_command_doctor)

    demo = subparsers.add_parser("demo", help="show offline demo payload")
    demo.add_argument("--offline", action="store_true", help="run offline-only checks")
    demo.add_argument(
        "--expect-provider",
        action="append",
        default=[],
        help="fail if the provider dependency is absent",
    )
    demo.set_defaults(func=_command_demo)

    scan = subparsers.add_parser("scan", help="run local system-first scan for a workspace")
    scan.add_argument("--workspace", required=True, help="workspace path to scan")
    scan.add_argument(
        "--expect-provider",
        action="append",
        default=[],
        help="fail if the provider dependency is absent",
    )
    scan.set_defaults(func=_command_scan)

    governor = subparsers.add_parser(
        "governor",
        help="report live memory/swap/RSS pressure and local-lane admission without killing processes",
    )
    governor.add_argument("--requested-lanes", type=int, default=0)
    governor.add_argument("--per-lane-peak-mib", type=int, default=1536)
    governor.add_argument("--max-local-lanes", type=int, default=1)
    governor.add_argument("--owned-pid", action="append", type=int, default=[])
    governor.add_argument("--timeout", type=float, default=20.0)
    governor.set_defaults(func=_command_governor)

    legacy_import = subparsers.add_parser(
        "legacy-import",
        help="audit legacy JSON runs and optionally import sanitized metadata into SQLite",
    )
    legacy_import.add_argument("--root", required=True, help="legacy runtime root")
    legacy_import.add_argument("--output-db", help="explicit new SQLite output path")
    legacy_import.add_argument("--reconcile", action="store_true", help="collect conservative local PID liveness evidence")
    legacy_import.add_argument("--max-runs", type=int, default=512)
    legacy_import.add_argument("--timeout", type=float, default=60.0)
    legacy_import.set_defaults(func=_command_legacy_import)

    cockpit = subparsers.add_parser(
        "cockpit",
        help="render a compact read-only L0 Mission Cockpit from saved snapshots",
    )
    cockpit.add_argument("--snapshot", required=True, help="SQLite/monitor snapshot JSON")
    cockpit.add_argument("--mission")
    cockpit.add_argument("--governor")
    cockpit.add_argument("--history")
    cockpit.add_argument("--timeout", type=float, default=20.0)
    cockpit.set_defaults(func=_command_cockpit)

    fit = subparsers.add_parser(
        "fit", help="match saved workload requirements to local/remote hardware facts"
    )
    fit.add_argument("--preflight", required=True, help="saved dispatch_preflight_scan JSON")
    fit.add_argument("--jobs", required=True, help="JSON list or {jobs: [...]} workload descriptions")
    fit.add_argument("--workspace", default=".", help="workspace used for local path facts")
    fit.set_defaults(func=_command_fit)

    estimate = subparsers.add_parser(
        "estimate", help="estimate task storage/CPU/GPU/network/runtime P50/P90 without execution"
    )
    estimate.add_argument("--task", required=True, help="task JSON path or a literal description")
    estimate.add_argument("--repo-root", help="bounded source metadata root")
    estimate.add_argument("--manifest", help="precomputed bounded manifest JSON")
    estimate.add_argument("--history", help="historical observations JSON")
    estimate.set_defaults(func=_command_estimate)

    capture = subparsers.add_parser(
        "capture", help="capture a provider-free TaskPacket, DAG, and history calibration"
    )
    capture.add_argument("--task", required=True, help="task JSON path or a literal description")
    capture.add_argument("--repo-root", help="bounded source metadata root")
    capture.add_argument("--manifest", help="precomputed bounded manifest JSON")
    capture.add_argument("--git-metadata", help="caller-supplied git metadata JSON")
    capture.add_argument("--policy", help="allow-listed user policy JSON")
    capture.add_argument("--history", help="historical observations JSON")
    capture.add_argument("--model")
    capture.add_argument("--host")
    capture.set_defaults(func=_command_capture)

    evidence = subparsers.add_parser(
        "evidence",
        help="build a search-first provider/host compatibility plan without probing",
    )
    evidence.add_argument("--provider", required=True)
    evidence.add_argument("--capability", required=True)
    evidence.add_argument("--version", dest="provider_version")
    evidence.add_argument("--model")
    evidence.add_argument("--host")
    evidence.add_argument(
        "--official-domain", action="append", default=[],
        help="official domain hint; may be repeated",
    )
    evidence.add_argument(
        "--sources",
        help="optional redacted evidence JSON (list or {records: [...]}) to resolve",
    )
    evidence.add_argument("--now", help="override resolution time for deterministic replay")
    evidence.set_defaults(func=_command_evidence)

    dispatch = subparsers.add_parser(
        "dispatch",
        help="run system-first preflight, estimate, fit, and multi-lane planning without provider execution",
    )
    dispatch.add_argument("--workspace", default=".")
    dispatch.add_argument(
        "--jobs", required=True,
        help="path to a JSON file containing a list or {jobs: [...]} workload descriptions",
    )
    dispatch.add_argument(
        "--preflight",
        help="saved preflight JSON (default provider-free mode; no provider contact)",
    )
    dispatch.add_argument(
        "--inventory",
        help="private host inventory used only with --live-probes",
    )
    dispatch.add_argument("--model-state")
    dispatch.add_argument("--runtime-state")
    dispatch.add_argument("--manifest", help="bounded task manifest JSON")
    dispatch.add_argument("--history", help="historical task observations JSON")
    dispatch.add_argument("--output", help="write the versioned report to this path")
    dispatch.add_argument(
        "--adapters",
        help="adapter registry required only with --enqueue/--execute",
    )
    dispatch.add_argument(
        "--db",
        help="SQLite database path required only with --enqueue/--execute",
    )
    dispatch.add_argument("--timeout", type=float, default=30.0)
    dispatch.add_argument("--max-lanes", type=int, default=4)
    dispatch.add_argument("--horizon", type=int, default=8)
    dispatch.add_argument(
        "--live-probes",
        action="store_true",
        help="explicitly discover provider/SSH state; still sends no model prompt and executes no provider",
    )
    dispatch.add_argument(
        "--skip-antigravity-usage",
        action="store_true",
        help="skip Antigravity /usage during explicit live discovery",
    )
    dispatch.add_argument(
        "--enqueue", "--execute", dest="enqueue", action="store_true",
        help="explicitly bridge and enqueue into SQLite; never executes a provider",
    )
    dispatch.set_defaults(func=_command_dispatch)

    preflight = subparsers.add_parser(
        "preflight", help="run local-first provider and SSH readiness discovery"
    )
    preflight.add_argument("--workspace", default=".")
    preflight.add_argument("--inventory", required=True, help="private SSH host inventory JSON")
    preflight.add_argument("--output", help="private preflight snapshot output")
    preflight.add_argument("--model-state")
    preflight.add_argument("--runtime-state")
    preflight.add_argument("--timeout", type=float, default=30.0)
    preflight.add_argument(
        "--skip-antigravity-usage", action="store_true",
        help="skip the interactive Antigravity /usage snapshot",
    )
    preflight.set_defaults(func=_command_preflight)

    bridge = subparsers.add_parser(
        "bridge", help="convert an approved plan to reviewable controller packets"
    )
    bridge.add_argument("--plan", required=True)
    bridge.add_argument("--jobs", required=True)
    bridge.add_argument("--state", required=True)
    bridge.add_argument("--adapters", required=True)
    bridge.add_argument("--mode", choices=("dry-run", "enqueue-ready"), default="dry-run")
    bridge.add_argument(
        "--enqueue", "--execute", dest="enqueue", action="store_true",
        help="explicitly enqueue validated packets into SQLite; never executes a provider",
    )
    bridge.add_argument("--db", help="SQLite database path required with --enqueue/--execute")
    bridge.set_defaults(func=_command_bridge)

    plan = subparsers.add_parser(
        "plan", help="build a rolling-horizon plan from saved state and jobs"
    )
    plan.add_argument("--state", required=True)
    plan.add_argument("--jobs", required=True)
    plan.add_argument("--max-lanes", type=int, default=4)
    plan.add_argument("--horizon", type=int, default=8)
    plan.set_defaults(func=_command_plan)

    run = subparsers.add_parser("run", help="run the durable controller for a prepared run")
    run.add_argument(
        "--backend", choices=("auto", "json", "sqlite"), default="auto",
        help="controller backend; auto uses SQLite for new runs and preserves existing JSON runs",
    )
    run.add_argument("--run-dir")
    run.add_argument("--db")
    run.add_argument("--workspace", default=".")
    run.add_argument("--inventory")
    run.add_argument("--runtime-state")
    run.add_argument("--once", action="store_true")
    run.add_argument("--poll-seconds", type=int, default=15)
    run.add_argument("--max-idle-rounds", type=int, default=0)
    run.add_argument("--max-lanes", type=int, default=1)
    run.add_argument("--owner-id")
    run.add_argument(
        "--detach", action="store_true",
        help="start an independent controller process and return its PID/log paths",
    )
    run.add_argument("--log", help="detached controller log path")
    run.add_argument("--pid-file", help="detached controller metadata path")
    run.set_defaults(func=_command_run)

    resume = subparsers.add_parser("resume", help="reconcile artifacts after interruption")
    resume.add_argument(
        "--backend", choices=("auto", "json", "sqlite"), default="auto",
        help="controller backend; auto preserves a detected legacy JSON run",
    )
    resume.add_argument("--run-dir")
    resume.add_argument("--db")
    resume.add_argument("--workspace", default=".", help="workspace used by the auto SQLite path")
    resume.set_defaults(func=_command_resume)

    enqueue = subparsers.add_parser("enqueue", help="enqueue a prepared task packet")
    enqueue.add_argument(
        "--backend", choices=("auto", "json", "sqlite"), default="auto",
        help="controller backend; auto uses SQLite unless an existing JSON run is detected",
    )
    enqueue.add_argument("--run-dir")
    enqueue.add_argument("--db")
    enqueue.add_argument("--job-file", required=True)
    enqueue.set_defaults(func=_command_enqueue)

    status = subparsers.add_parser("status", help="show durable controller state")
    status.add_argument(
        "--backend", choices=("auto", "json", "sqlite"), default="auto",
        help="controller backend; auto uses SQLite by default",
    )
    status.add_argument("--run-dir")
    status.add_argument("--db")
    status.add_argument("--workspace", default=".", help="workspace used by the auto SQLite path")
    status.set_defaults(func=_command_status)

    monitor = subparsers.add_parser("monitor", help="observe workers and emit replan feedback")
    monitor.add_argument("--state", required=True)
    monitor.add_argument("--duration-seconds", type=float, default=180.0)
    monitor.add_argument("--interval-seconds", type=float, default=30.0)
    monitor.add_argument("--stall-seconds", type=float, default=120.0)
    monitor.add_argument(
        "--refresh-codex-usage", action=argparse.BooleanOptionalAction, default=False,
        help="explicitly refresh Codex app-server usage (may contact provider)",
    )
    monitor.add_argument(
        "--refresh-compute-hosts", action=argparse.BooleanOptionalAction, default=False,
        help="explicitly probe configured SSH compute hosts",
    )
    monitor.set_defaults(func=_command_monitor)

    monitor_state = subparsers.add_parser(
        "monitor-state", help="project SQLite/controller snapshot into monitor worker state"
    )
    source = monitor_state.add_mutually_exclusive_group(required=True)
    source.add_argument("--db", help="SQLite controller database")
    source.add_argument("--snapshot", help="saved SQLite snapshot JSON")
    monitor_state.set_defaults(func=_command_monitor_state)

    replan = subparsers.add_parser(
        "replan", help="turn monitor feedback into reviewable planner constraints"
    )
    replan.add_argument("--monitor-report", required=True)
    replan.add_argument("--jobs")
    replan.add_argument("--plan")
    replan.add_argument("--state", help="planner state for copy-on-write merge")
    replan.add_argument("--run-planner", action="store_true", help="run the next deterministic plan")
    replan.add_argument("--max-lanes", type=int, default=4)
    replan.add_argument("--horizon", type=int, default=8)
    replan.add_argument("--apply", action="store_true", help="write explicit merged output paths")
    replan.add_argument("--merged-state-out")
    replan.add_argument("--merged-jobs-out")
    replan.add_argument("--next-plan-out")
    replan.set_defaults(func=_command_replan)

    closed_loop = subparsers.add_parser(
        "closed-loop",
        aliases=["loop"],
        help="consume an explicitly approved packet wave, then monitor and replan read-only",
    )
    closed_loop.add_argument(
        "--approved-packets",
        required=True,
        help="approved bridge packet bundle JSON (use - for stdin)",
    )
    closed_loop.add_argument(
        "--approved",
        action="store_true",
        help="explicitly attest that the packet bundle was reviewed",
    )
    closed_loop.add_argument(
        "--fake-execute",
        action="store_true",
        help="run only provider=fake local Python attempts; required for SQLite mutation",
    )
    closed_loop.add_argument("--db", help="SQLite path required with --fake-execute")
    closed_loop.add_argument("--workspace", default=".")
    closed_loop.add_argument("--max-lanes", type=int, default=1)
    closed_loop.add_argument("--monitor-duration-seconds", type=float, default=0.0)
    closed_loop.add_argument("--monitor-interval-seconds", type=float, default=0.1)
    closed_loop.add_argument("--monitor-stall-seconds", type=float, default=120.0)
    closed_loop.add_argument("--jobs", help="optional original planner jobs for a read-only next plan")
    closed_loop.add_argument("--state", help="optional planner state for a read-only next plan")
    closed_loop.add_argument("--plan", help="optional original dispatch plan for replan provenance")
    closed_loop.add_argument("--output", help="optional report output path")
    closed_loop.set_defaults(func=_command_closed_loop)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        return _command_version()

    if not hasattr(args, "func"):
        parser.print_help()
        return 1

    # fail-fast when unknown provider names are requested
    for provider in getattr(args, "expect_provider", []):
        if provider and provider not in OFFLINE_PROVIDERS and not shutil.which(provider):
            print(f"warning: non-standard provider requested: {provider}", file=sys.stderr)

    return int(args.func(args))  # type: ignore[union-attr]


if __name__ == "__main__":
    raise SystemExit(main())
