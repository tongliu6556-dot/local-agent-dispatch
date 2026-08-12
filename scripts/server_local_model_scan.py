#!/usr/bin/env python3
"""Lightly scan local/SSH hosts for local-model runtimes and loopback APIs."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import pathlib
import shlex
import subprocess
import sys
import time
from typing import Any


DEFAULT_ROOT = pathlib.Path(
    os.environ.get(
        "LOCAL_AGENT_DISPATCH_HOME",
        str(pathlib.Path.home() / ".codex" / "local-agent-dispatch"),
    )
)


REMOTE_SCANNER = r'''
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import urllib.error
import urllib.request

runtime_commands = (
    "vllm", "ollama", "llama-server", "llama-cli", "lmdeploy",
    "text-generation-launcher", "aider", "opencode",
)
managed_commands = {
    "vllm": pathlib.Path("venvs/vllm/bin/vllm"),
    "aider": pathlib.Path("venvs/aider/bin/aider"),
}
python_modules = ("vllm", "ollama", "llama_cpp", "lmdeploy", "text_generation")

def run(argv, timeout=3):
    try:
        result = subprocess.run(
            argv, text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )
        return result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""

probe_path = pathlib.Path(os.environ.get("LAD_PROBE_PROJECT", ".")).expanduser().resolve()
try:
    declared_storage = json.loads(os.environ.get("LAD_PROBE_STORAGE", "[]"))
except (TypeError, ValueError):
    declared_storage = []
storage_paths = []
for item in declared_storage if isinstance(declared_storage, list) else []:
    value = item.get("path") if isinstance(item, dict) else item
    if isinstance(value, str) and value.strip():
        candidate = pathlib.Path(value).expanduser().resolve()
        if candidate not in storage_paths:
            storage_paths.append(candidate)
probe_roots = [probe_path, *storage_paths]
for candidate in (probe_path / "local-agent-dispatch", probe_path / "lad"):
    if candidate.is_dir() and candidate not in probe_roots:
        probe_roots.append(candidate)
managed_commands = {
    name: next((root / path for root in probe_roots if (root / path).is_file()), probe_path / path)
    for name, path in managed_commands.items()
}

def model_dirs():
    roots = [
        probe_path / "models",
        probe_path / "local-agent-dispatch/models",
        pathlib.Path.home() / ".cache/huggingface/hub",
        pathlib.Path.home() / ".cache/modelscope/hub",
        pathlib.Path.home() / ".ollama/models/manifests/registry.ollama.ai/library",
    ]
    for storage_root in storage_paths:
        roots.extend(
            [
                storage_root / "models",
                storage_root / "local-agent-dispatch/models",
                storage_root / ".cache/huggingface/hub",
                storage_root / ".cache/modelscope/hub",
                storage_root / ".ollama/models/manifests/registry.ollama.ai/library",
            ]
        )
    found = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            children = sorted(root.iterdir(), key=lambda value: value.name)[:100]
        except OSError:
            continue
        for child in children:
            if not child.is_dir():
                continue
            name = child.name
            if "huggingface" in str(root):
                if not name.startswith("models--"):
                    continue
                name = name.removeprefix("models--").replace("--", "/")
            found.append({"root": str(root), "name": name, "path": str(child)})
    return found

def listeners():
    output = run(["ss", "-ltnp"]) if shutil.which("ss") else ""
    if not output and shutil.which("lsof"):
        output = run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"])
    rows = []
    for line in output.splitlines():
        if any(f":{port}" in line for port in (8000, 8080, 11434, 23333, 30000)):
            rows.append(line.strip()[:1000])
    return rows

def probe_json(url):
    try:
        with urllib.request.urlopen(url, timeout=0.7) as response:
            body = response.read(1024 * 1024).decode("utf-8", errors="replace")
        return json.loads(body)
    except (OSError, ValueError, urllib.error.URLError):
        return None

def agentic_smoke():
    for root in probe_roots:
        path = root / "run/agentic-smoke.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                payload.setdefault("evidence_root", str(root))
                return payload
        except (OSError, ValueError):
            continue
    return None

apis = []
for port in (8000, 8080, 11434, 23333, 30000):
    openai = probe_json(f"http://127.0.0.1:{port}/v1/models")
    if isinstance(openai, dict):
        model_ids = [str(row.get("id")) for row in (openai.get("data") or []) if row.get("id")]
        if model_ids:
            apis.append({
                "runtime": "openai_compatible", "base_url": f"http://127.0.0.1:{port}/v1",
                "models": model_ids, "health": "ready",
            })
            continue
    ollama = probe_json(f"http://127.0.0.1:{port}/api/tags")
    if isinstance(ollama, dict):
        model_ids = [
            str(row.get("model") or row.get("name"))
            for row in (ollama.get("models") or []) if row.get("model") or row.get("name")
        ]
        apis.append({
            "runtime": "ollama", "base_url": f"http://127.0.0.1:{port}",
            "models": model_ids, "health": "ready",
        })

# Never persist full argv: coding-agent prompts, bearer tokens, and workspace
# paths can appear there. PID plus normalized command name is sufficient for a
# coarse runtime inventory; detailed process attribution belongs to the
# controller's redacted snapshot.
process_output = run(["ps", "-eo", "pid=,comm="], timeout=4)
needles = ("vllm", "ollama", "llama-server", "lmdeploy", "text-generation-launcher", "aider", "opencode")
processes = [
    line.strip()[:300] for line in process_output.splitlines()
    if any(needle in line.lower() for needle in needles)
][:30]

payload = {
    "reported_hostname": run(["hostname"]).strip() or None,
    "runtime_commands": {
        name: shutil.which(name) or str(managed_commands[name])
        for name in runtime_commands
        if shutil.which(name) or (name in managed_commands and managed_commands[name].is_file())
    },
    "python_modules": [name for name in python_modules if importlib.util.find_spec(name)],
    "active_processes": processes,
    "listeners": listeners(),
    "apis": apis,
    "model_directories": model_dirs(),
    "agentic_smoke": agentic_smoke(),
}
print(json.dumps(payload, ensure_ascii=False))
'''


def load_json(path: str) -> Any:
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


def ssh_argv(host: dict[str, Any], timeout: float) -> list[str]:
    hostname = str(host.get("hostname") or "")
    if not hostname or any(char in hostname for char in "\n\r\0"):
        raise ValueError("SSH host requires a safe hostname")
    user = str(host.get("user") or "")
    target = f"{user}@{hostname}" if user else hostname
    argv = [
        "ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={max(1, int(timeout))}",
        "-o", "ServerAliveInterval=3", "-o", "ServerAliveCountMax=1",
    ]
    if host.get("port"):
        argv.extend(["-p", str(int(host["port"]))])
    if host.get("identity_file"):
        argv.extend(["-i", str(pathlib.Path(str(host["identity_file"])).expanduser())])
    argv.append(target)
    return argv


def scan_host(host: dict[str, Any], timeout: float) -> tuple[str, dict[str, Any]]:
    host_id = str(host.get("host_id") or "")
    transport = str(host.get("transport") or ("ssh" if host.get("hostname") else "local"))
    if not host_id:
        raise ValueError("every host requires host_id")
    argv = [sys.executable, "-"] if transport == "local" else ssh_argv(host, timeout) + ["python3 -"]
    project_path = str(host.get("project_path") or ".")
    if any(char in project_path for char in "\n\r\0"):
        raise ValueError(f"unsafe project_path for {host_id}")
    # Pass the declared host worktree through the script's stdin rather than
    # relying on SSH environment forwarding (which is commonly disabled).
    # The path is represented as a Python literal, never interpolated into a
    # shell command.  This also keeps the remote scanner self-contained.
    scanner = (
        "import os\n"
        f"os.environ['LAD_PROBE_PROJECT'] = {project_path!r}\n"
        f"os.environ['LAD_PROBE_STORAGE'] = {json.dumps(host.get('storage_paths') or host.get('storage_candidates') or [])!r}\n"
        f"{REMOTE_SCANNER}"
    )
    error = "scan did not run"
    attempts = 3 if transport == "ssh" else 1
    for attempt in range(attempts):
        try:
            completed = subprocess.run(
                argv, input=scanner, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=max(5.0, timeout), check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr.strip()[-1200:] or f"exit {completed.returncode}")
            parsed = json.loads(completed.stdout)
            parsed.update(
                host_id=host_id,
                transport=transport,
                hostname=host.get("hostname"),
                port=host.get("port"),
                reachable=True,
                error=None,
                attempts=attempt + 1,
            )
            return host_id, parsed
        except (OSError, subprocess.TimeoutExpired, ValueError, RuntimeError) as exc:
            error = str(exc)
            if attempt + 1 < attempts:
                time.sleep(1.0)
    return host_id, {
        "host_id": host_id, "transport": transport, "hostname": host.get("hostname"),
        "port": host.get("port"), "reachable": False, "error": error, "attempts": attempts,
        "runtime_commands": {}, "python_modules": [], "active_processes": [],
        "listeners": [], "apis": [], "model_directories": [],
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", default=str(DEFAULT_ROOT / "hosts.json"))
    parser.add_argument("--output")
    parser.add_argument("--timeout", type=float, default=10.0)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        inventory = load_json(args.inventory)
        hosts = inventory.get("hosts", inventory) if isinstance(inventory, dict) else inventory
        if isinstance(hosts, dict):
            hosts = [dict(value or {}, host_id=key) for key, value in hosts.items()]
        if not isinstance(hosts, list):
            raise ValueError("inventory must contain a hosts list or object")
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(hosts)))) as executor:
            rows = dict(executor.map(lambda item: scan_host(item, args.timeout), hosts))
        ready_apis = sum(len(row.get("apis") or []) for row in rows.values())
        payload = {
            "ok": True,
            "scanned_at_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
            "inventory": str(pathlib.Path(args.inventory).expanduser()),
            "hosts": rows,
            "ready_api_count": ready_apis,
        }
    except Exception as exc:
        payload = {"ok": False, "error": str(exc)}
    atomic_write(args.output, payload)
    return 0 if payload.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
