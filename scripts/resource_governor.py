#!/usr/bin/env python3
"""Provider-free local memory admission and degradation controller.

This module deliberately does not kill arbitrary processes.  It observes
resident memory, swap/compressor pressure, and canonical process names, then
returns a deterministic admission/action plan.  Only processes explicitly
owned by a dispatch controller may be candidates for a later pause/resume
integration; unowned Codex, MCP, IDE, and desktop processes are advisory only.

The controller is intentionally separate from the planner.  The planner
decides *where* work should go; this module decides whether another local lane
is safe *now*, with hysteresis-friendly tiers that a monitor can feed back into
the next planning wave.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import platform
import re
import shutil
import subprocess
import sys
from typing import Any, Iterable


SCHEMA_VERSION = 1
GIB = 1024**3
MIB = 1024**2


def utc_now() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).isoformat()


def _number(value: Any) -> float | None:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _int(value: Any) -> int | None:
    parsed = _number(value)
    return None if parsed is None else int(parsed)


def _basename(value: str) -> str:
    # ``ps comm`` is expected to be argument-free, but some platforms/tests
    # may append a display suffix.  Keep only the executable token so prompt
    # text can never enter the persisted process record.
    token = value.strip().strip("[]").split(None, 1)[0]
    return token.replace("\\", "/").rsplit("/", 1)[-1].lower()


def classify_process_name(raw_name: str) -> str | None:
    """Map only safe executable names; never inspect or persist argv."""
    name = _basename(raw_name)
    if "codebase-memory-mcp" in name:
        return "codebase_memory_mcp"
    if "cursor" in name and "mcp" in name:
        return "cursor_mcp"
    if name == "codex" or name.startswith("codex-") or "codex" in name:
        return "codex"
    if name == "antigravity" or "antigravity" in name:
        return "antigravity"
    if name == "opencode" or "opencode" in name:
        return "opencode"
    if name in {"cursor-agent", "cursor"}:
        return "cursor"
    if name in {"node", "nodejs"}:
        return "node"
    if name.startswith("python"):
        return "python"
    if name in {"ollama", "vllm", "llama-server", "llama_cpp_server", "local-ai", "localai"}:
        return "model_runtime"
    return None


def parse_unix_processes(text: str, current_pid: int | None = None) -> list[dict[str, Any]]:
    """Parse ``ps`` rows while dropping every argument and raw command line."""
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for line in text.splitlines():
        fields = line.strip().split(None, 4)
        if len(fields) < 5 or not fields[0].isdigit():
            continue
        pid = int(fields[0])
        if current_pid is not None and pid == current_pid:
            continue
        if pid in seen:
            continue
        rss_kib = _int(fields[1])
        vsz_kib = _int(fields[2])
        cpu = _number(fields[3])
        process_class = classify_process_name(fields[4])
        if process_class is None:
            continue
        row: dict[str, Any] = {
            "pid": pid,
            "process_class": process_class,
            "command_name": _basename(fields[4]),
        }
        if rss_kib is not None:
            row["rss_bytes"] = rss_kib * 1024
        if vsz_kib is not None:
            row["vsz_bytes"] = vsz_kib * 1024
        if cpu is not None:
            row["cpu_percent"] = cpu
        rows.append(row)
        seen.add(pid)
    return sorted(rows, key=lambda row: (-int(row.get("rss_bytes") or 0), int(row["pid"])))


def _ratio(numerator: int | float | None, denominator: int | float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * float(numerator) / float(denominator)))


def pressure_tier(
    *,
    total_bytes: int | None,
    available_bytes: int | None,
    swap_total_bytes: int | None,
    swap_used_bytes: int | None,
    pressure_state: str | None = None,
    policy: dict[str, Any] | None = None,
) -> str:
    """Return normal/conserve/critical/emergency using conservative gates."""
    policy = policy or {}
    available_percent = _ratio(available_bytes, total_bytes)
    swap_percent = _ratio(swap_used_bytes, swap_total_bytes)
    state = str(pressure_state or "unknown").lower()
    critical_available = float(policy.get("critical_available_percent", 15.0))
    emergency_available = float(policy.get("emergency_available_percent", 8.0))
    critical_swap = float(policy.get("critical_swap_used_percent", 95.0))
    conserve_available = float(policy.get("conserve_available_percent", 30.0))
    conserve_swap = float(policy.get("conserve_swap_used_percent", 85.0))
    if (
        state == "critical"
        or (available_percent is not None and available_percent <= emergency_available)
        or (available_percent is not None and available_percent <= critical_available and swap_percent is not None and swap_percent >= critical_swap)
    ):
        return "emergency" if available_percent is not None and available_percent <= emergency_available else "critical"
    if state == "conserve" or (
        available_percent is not None and available_percent <= conserve_available
    ) or (swap_percent is not None and swap_percent >= conserve_swap):
        return "conserve"
    if state == "unknown" and (available_percent is None or total_bytes is None):
        return "critical"
    return "normal"


def _sum_by_class(processes: Iterable[dict[str, Any]]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for row in processes:
        key = str(row.get("process_class") or "unknown")
        rss = _int(row.get("rss_bytes")) or 0
        totals[key] = totals.get(key, 0) + rss
    return dict(sorted(totals.items()))


def build_report(
    *,
    ram: dict[str, Any],
    processes: list[dict[str, Any]],
    requested_lanes: int = 0,
    per_lane_peak_bytes: int | None = None,
    max_local_lanes: int = 1,
    owned_pids: Iterable[int] = (),
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic, non-destructive resource-governor report."""
    policy = dict(policy or {})
    owned = {int(pid) for pid in owned_pids}
    normalized_processes: list[dict[str, Any]] = []
    for source in processes:
        row = dict(source)
        pid = _int(row.get("pid"))
        if pid is None:
            continue
        row["pid"] = pid
        row["owned_by_dispatch"] = pid in owned
        normalized_processes.append(row)
    total = _int(ram.get("total_bytes"))
    available = _int(ram.get("available_bytes"))
    swap_total = _int(ram.get("swap_total_bytes"))
    swap_used = _int(ram.get("swap_used_bytes"))
    tier = pressure_tier(
        total_bytes=total,
        available_bytes=available,
        swap_total_bytes=swap_total,
        swap_used_bytes=swap_used,
        pressure_state=ram.get("pressure_state"),
        policy=policy,
    )
    reserve_percent = float(policy.get("reserve_percent", 25.0))
    reserve_bytes = max(int((total or 0) * reserve_percent / 100.0), _int(policy.get("reserve_bytes")) or 0)
    headroom = max(0, (available or 0) - reserve_bytes)
    lane_peak = _int(per_lane_peak_bytes)
    if lane_peak and lane_peak > 0:
        capacity_lanes = max(0, headroom // lane_peak)
    else:
        capacity_lanes = None
    hard_local_block = tier in {"conserve", "critical", "emergency"}
    if hard_local_block:
        admissible = 0
    elif capacity_lanes is None:
        admissible = max(0, int(max_local_lanes))
    else:
        admissible = min(max(0, int(max_local_lanes)), int(capacity_lanes))
    requested = max(0, int(requested_lanes))
    estimated_peak = None
    if lane_peak is not None:
        estimated_peak = (sum(_int(row.get("rss_bytes")) or 0 for row in normalized_processes) + requested * lane_peak)
    actions: list[dict[str, Any]] = []
    if tier == "normal":
        decision = "admit" if requested <= admissible else "throttle"
        if decision == "throttle":
            actions.append({"action": "reduce_local_lanes", "to": admissible, "reason": "reserved_headroom"})
    elif tier == "conserve":
        decision = "throttle"
        actions.extend(
            [
                {"action": "block_new_local_lanes", "reason": "swap_or_available_memory_pressure"},
                {"action": "route_compatible_work_remote", "reason": "preserve_local_headroom"},
            ]
        )
    elif tier == "critical":
        decision = "pause_owned"
        actions.extend(
            [
                {"action": "block_new_local_lanes", "reason": "critical_memory_pressure"},
                {"action": "pause_owned_lanes", "pids": sorted(pid for pid in owned if pid > 0), "requires_controller_lease": True},
                {"action": "route_compatible_work_remote", "reason": "critical_memory_pressure"},
            ]
        )
    else:
        decision = "emergency_pause_owned"
        actions.extend(
            [
                {"action": "block_new_local_lanes", "reason": "emergency_memory_pressure"},
                {"action": "pause_owned_lanes", "pids": sorted(pid for pid in owned if pid > 0), "requires_controller_lease": True},
                {"action": "do_not_kill_unowned_processes", "reason": "ownership_not_proven"},
            ]
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "report_type": "local-agent-dispatch.resource-governor",
        "observed_at_utc": utc_now(),
        "read_only": True,
        "provider_execution": False,
        "ram": {
            "total_bytes": total,
            "available_bytes": available,
            "available_percent": _ratio(available, total),
            "swap_total_bytes": swap_total,
            "swap_used_bytes": swap_used,
            "swap_used_percent": _ratio(swap_used, swap_total),
            "pressure_state": ram.get("pressure_state"),
            "pressure_tier": tier,
            "reserve_bytes": reserve_bytes,
            "headroom_bytes_after_reserve": headroom,
        },
        "processes": normalized_processes,
        "rss": {
            "total_bytes": sum(_int(row.get("rss_bytes")) or 0 for row in normalized_processes),
            "by_process_class_bytes": _sum_by_class(normalized_processes),
            "top_processes": normalized_processes[:10],
        },
        "request": {
            "requested_lanes": requested,
            "per_lane_peak_bytes": lane_peak,
            "estimated_peak_bytes": estimated_peak,
            "max_local_lanes_policy": max(0, int(max_local_lanes)),
        },
        "admission": {
            "decision": decision,
            "local_agent_launch_allowed": not hard_local_block and admissible > 0,
            "max_new_local_lanes": int(admissible),
            "capacity_lanes_from_headroom": capacity_lanes,
            "owned_pids": sorted(owned),
            "unowned_pressure_is_advisory": True,
        },
        "actions": actions,
        "safety": {
            "automatic_kill": False,
            "automatic_signal": False,
            "owned_only_for_future_pause": True,
            "requires_controller_lease": True,
            "argv_collected": False,
        },
        "next_poll_seconds": int(policy.get("poll_seconds", 30)),
    }


def _run(argv: list[str], timeout: float = 4.0) -> str:
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            check=False,
            env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")},
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout or ""


def observe_local(*, timeout: float = 4.0, current_pid: int | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Collect local memory and canonical process telemetry without argv."""
    try:
        import local_system_scan as scanner
    except ImportError:
        scanner = None
    system_name = platform.system() or "Unknown"
    if scanner is not None:
        ram = scanner.scan_ram(system_name, timeout)
    else:
        ram = {}
    ps = shutil.which("ps")
    if not ps:
        return ram, []
    args = [ps, "-axo", "pid=,rss=,vsz=,%cpu=,comm="] if system_name == "Darwin" else [ps, "-eo", "pid=,rss=,vsz=,%cpu=,comm="]
    return ram, parse_unix_processes(_run(args, timeout), current_pid or os.getpid())


def _load_json(path: str) -> dict[str, Any]:
    if path == "-":
        payload = json.load(sys.stdin)
    else:
        with pathlib.Path(path).expanduser().open(encoding="utf-8") as handle:
            payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("JSON object required")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--system-scan", help="saved local_system_scan JSON, or - for stdin")
    parser.add_argument("--requested-lanes", type=int, default=0)
    parser.add_argument("--per-lane-peak-mib", type=int, default=1536)
    parser.add_argument("--max-local-lanes", type=int, default=1)
    parser.add_argument("--owned-pid", action="append", type=int, default=[])
    parser.add_argument("--output", default="-")
    args = parser.parse_args(argv)
    if args.system_scan:
        snapshot = _load_json(args.system_scan)
        ram = snapshot.get("ram") or {}
        process_rows = []
        # The base system scanner intentionally excludes non-provider MCP
        # processes.  A live governor scan adds the broader, still argv-free
        # process view unless a caller supplies one explicitly.
        if isinstance(snapshot.get("resource_governor_processes"), list):
            process_rows = snapshot["resource_governor_processes"]
        else:
            _, process_rows = observe_local()
    else:
        ram, process_rows = observe_local()
    report = build_report(
        ram=ram,
        processes=process_rows,
        requested_lanes=args.requested_lanes,
        per_lane_peak_bytes=max(0, args.per_lane_peak_mib) * MIB,
        max_local_lanes=max(0, args.max_local_lanes),
        owned_pids=args.owned_pid,
    )
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output == "-":
        print(text, end="")
    else:
        target = pathlib.Path(args.output).expanduser()
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
