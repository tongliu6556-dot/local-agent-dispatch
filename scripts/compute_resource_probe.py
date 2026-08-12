#!/usr/bin/env python3
"""Probe local and SSH compute hosts into scheduler-ready JSON state."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import pathlib
import shlex
import subprocess
import sys
import time
from typing import Any


REMOTE_PROBE = r'''
probe_path=$1
shift
os_name=$(uname -s 2>/dev/null || printf unknown)
arch=$(uname -m 2>/dev/null || printf unknown)
host_name=$(hostname 2>/dev/null || printf unknown)
printf 'META|%s|%s|%s\n' "$host_name" "$os_name" "$arch"

if [ "$os_name" = Darwin ]; then
  logical=$(sysctl -n hw.logicalcpu 2>/dev/null || printf 0)
  physical=$(sysctl -n hw.physicalcpu 2>/dev/null || printf 0)
  total_bytes=$(sysctl -n hw.memsize 2>/dev/null || printf 0)
  page_size=$(sysctl -n hw.pagesize 2>/dev/null || printf 4096)
  vm_values=$(vm_stat 2>/dev/null | awk '
    /Pages free/ {gsub("\\.", "", $3); free=$3}
    /Pages inactive/ {gsub("\\.", "", $3); inactive=$3}
    /Pages speculative/ {gsub("\\.", "", $3); speculative=$3}
    /Pages purgeable/ {gsub("\\.", "", $3); purgeable=$3}
    END {printf "%d", free+inactive+speculative+purgeable}')
  available_bytes=$((vm_values * page_size))
  load1=$(sysctl -n vm.loadavg 2>/dev/null | awk '{print $2}')
  cpu_model=$(sysctl -n machdep.cpu.brand_string 2>/dev/null || printf Apple)
else
  logical=$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || printf 0)
  physical=$(lscpu -p=core,socket 2>/dev/null | awk -F, '!/^#/ {seen[$1 FS $2]=1} END {print length(seen)}')
  [ -n "$physical" ] || physical=$logical
  total_kb=$(awk '/MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null)
  available_kb=$(awk '/MemAvailable:/ {print $2}' /proc/meminfo 2>/dev/null)
  total_bytes=$((${total_kb:-0} * 1024))
  available_bytes=$((${available_kb:-0} * 1024))
  load1=$(cut -d' ' -f1 /proc/loadavg 2>/dev/null || printf 0)
  cpu_model=$(lscpu 2>/dev/null | awk -F: '/Model name/ {sub(/^[ 	]+/, "", $2); print $2; exit}')
fi
printf 'CPU|%s|%s|%s|%s\n' "${logical:-0}" "${physical:-0}" "${load1:-0}" "${cpu_model:-unknown}"
printf 'MEM|%s|%s\n' "${total_bytes:-0}" "${available_bytes:-0}"

probe_disk() {
  candidate=$1
  [ -n "$candidate" ] || return 0
  if [ -d "$candidate" ]; then
    disk_line=$(df -Pk "$candidate" 2>/dev/null | tail -n 1)
    disk_total=$(printf '%s\n' "$disk_line" | awk '{print $2 * 1024}')
    disk_available=$(printf '%s\n' "$disk_line" | awk '{print $4 * 1024}')
    if [ -w "$candidate" ]; then writable=1; else writable=0; fi
    printf 'DISK|%s|1|%s|%s|%s\n' "$candidate" "${disk_total:-0}" "${disk_available:-0}" "$writable"
  else
    printf 'DISK|%s|0|0|0|0\n' "$candidate"
  fi
}

# The declared project path remains the primary compatibility field.  Extra
# paths let a container use its real data/work volume rather than assuming
# that / (or /root) is the only capacity.  All checks are read-only df/test.
probe_disk "$probe_path"
for candidate in "$@"; do
  [ "$candidate" = "__LAD_DISCOVER_STORAGE__" ] && continue
  [ "$candidate" = "$probe_path" ] && continue
  probe_disk "$candidate"
done
for candidate in /workspace /data /mnt /scratch /work; do
  discover=0
  for requested in "$@"; do
    [ "$requested" = "__LAD_DISCOVER_STORAGE__" ] && discover=1
  done
  [ "$discover" = 1 ] || break
  [ "$candidate" = "$probe_path" ] && continue
  probe_disk "$candidate"
done

if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=index,name,memory.total,memory.free,utilization.gpu,driver_version \
    --format=csv,noheader,nounits 2>/dev/null | while IFS= read -r gpu_line; do
      printf 'GPU|%s\n' "$gpu_line"
    done
elif [ "$os_name" = Darwin ]; then
  system_profiler SPDisplaysDataType 2>/dev/null | awk -F': ' '
    /Chipset Model:/ {name=$2}
    /Total Number of Cores:/ {printf "APPLE_GPU|%s|%s\n", name, $2; exit}'
fi

for command_name in python3 conda docker nvidia-smi codex-racknerd-route codex-large-download; do
  command_path=$(command -v "$command_name" 2>/dev/null || true)
  if [ -z "$command_path" ] && [ -x "$HOME/.local/bin/$command_name" ]; then
    command_path="$HOME/.local/bin/$command_name"
  fi
  [ -z "$command_path" ] || printf 'CMD|%s|%s\n' "$command_name" "$command_path"
done
python_path=$(command -v python3 2>/dev/null || true)
if [ -n "$python_path" ]; then
  python_version=$(python3 -c 'import platform; print(platform.python_version())' 2>/dev/null || true)
  printf 'PYTHON|%s|%s\n' "$python_path" "$python_version"
fi
'''


def load_json(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    return json.loads(pathlib.Path(path).expanduser().read_text(encoding="utf-8"))


def atomic_write(path: str | None, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if not path:
        print(text, end="")
        return
    target = pathlib.Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(target)


def gib(value: str) -> float:
    try:
        return round(int(float(value)) / (1024**3), 3)
    except (TypeError, ValueError):
        return 0.0


def parse_gpu(line: str) -> dict[str, Any] | None:
    parts = [part.strip() for part in line.split(",")]
    if len(parts) < 6:
        return None
    try:
        total = round(float(parts[2]) / 1024, 3)
        free = round(float(parts[3]) / 1024, 3)
        utilization = float(parts[4])
    except ValueError:
        return None
    return {
        "index": int(parts[0]) if parts[0].isdigit() else parts[0],
        "name": parts[1],
        "vram_total_gib": total,
        "vram_free_gib": free,
        "utilization_percent": utilization,
        "driver_version": parts[5],
    }


def parse_output(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {"gpus": [], "commands": {}, "disks": []}
    for raw in text.splitlines():
        parts = raw.split("|")
        if not parts:
            continue
        tag = parts[0]
        if tag == "META" and len(parts) >= 4:
            # Keep the SSH connection hostname from the inventory. Container
            # hostnames are diagnostic facts and are usually not externally resolvable.
            result.update(reported_hostname=parts[1], os=parts[2], arch=parts[3])
        elif tag == "CPU" and len(parts) >= 5:
            try:
                logical = int(parts[1])
                physical = int(parts[2])
                load1 = float(parts[3] or 0)
            except ValueError:
                continue
            result.update(
                logical_cpu_cores=logical,
                physical_cpu_cores=physical,
                load1=load1,
                estimated_idle_cpu_cores=max(0, int(logical - load1)),
                cpu_model="|".join(parts[4:]),
            )
        elif tag == "MEM" and len(parts) >= 3:
            result.update(memory_total_gib=gib(parts[1]), memory_available_gib=gib(parts[2]))
        elif tag == "DISK" and len(parts) >= 6:
            path = parts[1]
            row = {
                "path": path,
                "exists": parts[2] == "1",
                "disk_total_gib": gib(parts[3]),
                "disk_free_gib": gib(parts[4]),
                "writable": parts[5] == "1",
            }
            result["disks"].append(row)
        elif tag == "GPU" and len(parts) >= 2:
            gpu = parse_gpu("|".join(parts[1:]))
            if gpu:
                result["gpus"].append(gpu)
        elif tag == "APPLE_GPU" and len(parts) >= 3:
            result["gpus"].append(
                {
                    "index": 0,
                    "name": parts[1],
                    "unified_memory": True,
                    "core_count": int(parts[2]) if parts[2].isdigit() else parts[2],
                }
            )
        elif tag == "CMD" and len(parts) >= 3:
            result["commands"][parts[1]] = "|".join(parts[2:])
        elif tag == "PYTHON" and len(parts) >= 3:
            result["python"] = {"path": parts[1], "version": parts[2]}
    result["gpu_count"] = len(result["gpus"])
    # Preserve the first/project disk fields consumed by existing planners,
    # while exposing every declared/discovered mount for placement decisions.
    disks = [row for row in result["disks"] if row.get("exists")]
    if disks:
        primary = disks[0]
        result.update(
            project_path_exists=bool(primary.get("exists")),
            disk_total_gib=primary.get("disk_total_gib", 0.0),
            disk_free_gib=primary.get("disk_free_gib", 0.0),
            project_path_writable=bool(primary.get("writable")),
        )
        writable = [row for row in disks if row.get("writable")]
        best = max(disks, key=lambda row: float(row.get("disk_free_gib") or 0.0))
        result["best_storage_path"] = best.get("path")
        result["best_writable_storage_path"] = max(writable, key=lambda row: float(row.get("disk_free_gib") or 0.0)).get("path") if writable else None
    else:
        result.update(project_path_exists=False, disk_total_gib=0.0, disk_free_gib=0.0, project_path_writable=False)
        result["best_storage_path"] = None
        result["best_writable_storage_path"] = None
    return result


def ssh_argv(host: dict[str, Any], timeout: float) -> list[str]:
    hostname = str(host.get("hostname") or "")
    if not hostname or any(char in hostname for char in "\n\r\0"):
        raise ValueError("SSH host requires a safe hostname")
    user = str(host.get("user") or "")
    target = f"{user}@{hostname}" if user else hostname
    argv = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={max(1, int(timeout))}",
        "-o", "ServerAliveInterval=3",
        "-o", "ServerAliveCountMax=1",
    ]
    if host.get("port"):
        argv.extend(["-p", str(int(host["port"]))])
    identity_file = host.get("identity_file")
    if identity_file:
        argv.extend(["-i", str(pathlib.Path(str(identity_file)).expanduser())])
    argv.append(target)
    return argv


def probe_host(host: dict[str, Any], timeout: float) -> tuple[str, dict[str, Any]]:
    host_id = str(host.get("host_id") or "")
    if not host_id:
        raise ValueError("every host requires host_id")
    transport = str(host.get("transport") or ("ssh" if host.get("hostname") else "local"))
    project_path = str(host.get("project_path") or ".")
    if any(char in project_path for char in "\n\r\0|"):
        raise ValueError(f"unsafe project_path for {host_id}")
    raw_storage = host.get("storage_paths") or host.get("storage_candidates") or []
    if isinstance(raw_storage, (str, pathlib.Path)):
        raw_storage = [str(raw_storage)]
    if not isinstance(raw_storage, list):
        raise ValueError(f"storage_paths for {host_id} must be a list")
    storage_paths: list[str] = []
    for item in raw_storage:
        value = item.get("path") if isinstance(item, dict) else item
        if not isinstance(value, str) or not value.strip() or any(char in value for char in "\n\r\0|"):
            raise ValueError(f"unsafe storage path for {host_id}")
        if value not in storage_paths and value != project_path:
            storage_paths.append(value)
    discover_storage = bool(host.get("discover_storage", transport == "ssh"))
    probe_args = [project_path, *storage_paths]
    if discover_storage:
        probe_args.append("__LAD_DISCOVER_STORAGE__")
    started = time.monotonic()
    if transport == "local":
        argv = ["sh", "-s", "--", *probe_args]
    elif transport == "ssh":
        argv = ssh_argv(host, timeout) + ["sh -s -- " + " ".join(shlex.quote(item) for item in probe_args)]
    else:
        raise ValueError(f"unsupported transport for {host_id}: {transport}")
    try:
        completed = subprocess.run(
            argv,
            input=REMOTE_PROBE,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(2.0, timeout),
            check=False,
        )
        reachable = completed.returncode == 0
        parsed = parse_output(completed.stdout) if reachable else {"gpus": [], "commands": {}, "disks": [], "gpu_count": 0}
        error = None if reachable else (completed.stderr.strip()[-1000:] or f"exit {completed.returncode}")
    except subprocess.TimeoutExpired:
        reachable = False
        parsed = {"gpus": [], "commands": {}, "disks": [], "gpu_count": 0}
        error = f"probe timeout after {timeout}s"
    result = dict(host)
    result.update(parsed)
    result.update(
        host_id=host_id,
        transport=transport,
        project_path=project_path,
        reachable=reachable,
        probe_latency_seconds=round(time.monotonic() - started, 3),
        last_probed_at_utc=dt.datetime.now(tz=dt.timezone.utc).isoformat(),
    )
    result["storage_paths"] = parsed.get("disks", [])
    result["storage_discovery"] = {
        "declared_paths": storage_paths,
        "common_mounts_scanned": discover_storage,
    }
    if error:
        result["probe_error"] = error
    if reachable:
        result["racknerd_route_helper"] = "codex-racknerd-route" in result.get("commands", {})
        result["large_download_helper"] = "codex-large-download" in result.get("commands", {})
    return host_id, result


def probe_inventory(payload: Any, timeout: float, workers: int) -> dict[str, Any]:
    hosts = payload.get("hosts", []) if isinstance(payload, dict) else payload
    if not isinstance(hosts, list) or not hosts:
        raise ValueError("inventory must be a host list or an object with a non-empty hosts list")
    compute_hosts: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(workers, len(hosts)))) as executor:
        futures = [executor.submit(probe_host, dict(host), timeout) for host in hosts]
        for future in concurrent.futures.as_completed(futures):
            host_id, result = future.result()
            compute_hosts[host_id] = result
    reachable = sum(1 for host in compute_hosts.values() if host.get("reachable"))
    return {
        "ok": True,
        "probed_at_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "reachable_hosts": reachable,
        "total_hosts": len(compute_hosts),
        "compute_hosts": dict(sorted(compute_hosts.items())),
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True, help="JSON inventory path or - for stdin")
    # Some hosted containers expose large AutoFS mounts (for example a
    # read-only data volume) whose first ``df`` may take several seconds.  An
    # 8-second default made a reachable GPU host look unavailable.  Keep the
    # probe bounded, but give the read-only mount inventory enough budget to
    # finish before fail-closed timeout handling marks the host unknown.
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        result = probe_inventory(load_json(args.inventory), max(2.0, args.timeout), max(1, args.workers))
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    atomic_write(args.output, result)
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
