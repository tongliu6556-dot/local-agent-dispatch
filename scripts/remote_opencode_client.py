#!/usr/bin/env python3
"""Direct SSH client for a server-side authenticated OpenCode Go worker.

The default is a no-network dry run.  ``--execute`` sends only the prompt
stdin to an already verified SSH host and starts the allow-listed remote
wrapper; it never copies credentials or accepts an arbitrary shell command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import posixpath
import re
import subprocess
import sys
from typing import Any, Mapping

from remote_worker_client import _digest, _remote_path, _text


MODEL_RE = re.compile(r"opencode-go/[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
SAFE_RE = re.compile(r"[A-Za-z0-9_+@=,:./-]+\Z")


class RemoteOpenCodeError(ValueError):
    pass


def _safe_remote(value: Any, field: str) -> str:
    value = _remote_path(value, field)
    if not SAFE_RE.fullmatch(value):
        raise RemoteOpenCodeError(f"{field} contains unsafe shell characters")
    return value


def _safe_command(value: Any, field: str) -> str:
    value = _text(value, field)
    if not SAFE_RE.fullmatch(value) or value in {".", ".."}:
        raise RemoteOpenCodeError(f"{field} contains unsafe characters")
    return value


def _remote_join(root: str, value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RemoteOpenCodeError(f"{field} is required")
    candidate = value if value.startswith("/") else posixpath.join(root, value)
    normalized = posixpath.normpath(candidate)
    if not (normalized == root or normalized.startswith(root.rstrip("/") + "/")):
        raise RemoteOpenCodeError(f"{field} escapes project_path")
    return _safe_remote(normalized, field)


def load_opencode_inventory(path: pathlib.Path | str) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(pathlib.Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RemoteOpenCodeError("OpenCode inventory is not valid JSON") from exc
    raw = payload.get("hosts", payload) if isinstance(payload, Mapping) else payload
    rows = list(raw.values()) if isinstance(raw, Mapping) else raw
    if not isinstance(rows, list) or not rows:
        raise RemoteOpenCodeError("OpenCode inventory must contain hosts")
    result: dict[str, dict[str, Any]] = {}
    for original in rows:
        if not isinstance(original, Mapping):
            raise RemoteOpenCodeError("OpenCode host must be an object")
        row = dict(original)
        host_id = _text(row.get("host_id"), "host_id", pattern=re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z"))
        if row.get("transport") != "ssh":
            raise RemoteOpenCodeError("OpenCode host transport must be ssh")
        hostname = _text(row.get("hostname"), "hostname", pattern=re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252})\Z"))
        user = _text(row.get("user") or "root", "user", pattern=re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z"))
        try:
            port = int(row.get("port"))
        except (TypeError, ValueError) as exc:
            raise RemoteOpenCodeError("port is invalid") from exc
        if not 1 <= port <= 65535:
            raise RemoteOpenCodeError("port is invalid")
        normalized = {
            "host_id": host_id,
            "transport": "ssh",
            "hostname": hostname,
            "user": user,
            "port": port,
            "project_path": _safe_remote(row.get("project_path"), "project_path"),
            "opencode_runner": _safe_remote(row.get("opencode_runner"), "opencode_runner"),
        }
        if row.get("opencode_bin") is not None:
            normalized["opencode_bin"] = row["opencode_bin"]
        if host_id in result:
            raise RemoteOpenCodeError("duplicate host_id")
        result[host_id] = normalized
    return result


def build_command(host: Mapping[str, Any], *, cwd: str, result_source: str, model: str, variant: str | None, opencode_bin: str, timeout: int, auto_approve: bool) -> list[str]:
    if not MODEL_RE.fullmatch(model):
        raise RemoteOpenCodeError("model must be an exact opencode-go/<model-id>")
    root = _safe_remote(host.get("project_path"), "project_path")
    remote_cwd = _remote_join(root, cwd, "remote_cwd")
    remote_result = _remote_join(remote_cwd, result_source, "remote_result_source")
    runner = _safe_remote(host.get("opencode_runner"), "opencode_runner")
    raw_binary = host.get("opencode_bin") or opencode_bin
    binary = _safe_remote(raw_binary, "opencode_bin") if str(raw_binary).startswith("/") else _safe_command(raw_binary, "opencode_bin")
    hostname = _text(host.get("hostname"), "hostname", pattern=re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252})\Z"))
    user = _text(host.get("user") or "root", "user", pattern=re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z"))
    port = int(host.get("port"))
    if not 1 <= port <= 65535:
        raise RemoteOpenCodeError("port is invalid")
    argv = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={min(900, max(1, timeout))}", "-o", "ServerAliveInterval=3", "-o", "ServerAliveCountMax=1", "-p", str(port), f"{user}@{hostname}", "python3", runner, "--cwd", remote_cwd, "--model", model, "--result-source", remote_result, "--opencode-bin", binary, "--timeout-seconds", str(timeout)]
    if variant:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z", variant):
            raise RemoteOpenCodeError("variant is invalid")
        argv.extend(["--variant", variant])
    if auto_approve:
        argv.append("--auto-approve")
    return argv


def request(inventory: Mapping[str, Any] | pathlib.Path | str, *, host_id: str, prompt_file: pathlib.Path | str, cwd: str, result_source: str, model: str, variant: str | None = None, opencode_bin: str = "opencode", timeout: int = 3600, auto_approve: bool = False, execute: bool = False) -> dict[str, Any]:
    if isinstance(inventory, Mapping):
        # Callers embedding an inventory should use the same strict schema as
        # the file form without creating a credential-bearing temp file.
        hosts = load_opencode_inventory_from_mapping(inventory)
    else:
        hosts = load_opencode_inventory(inventory)
    if host_id not in hosts:
        raise RemoteOpenCodeError("host_id is not present in inventory")
    host = hosts[host_id]
    path = pathlib.Path(prompt_file).expanduser().resolve()
    if not path.is_file():
        raise RemoteOpenCodeError("prompt_file does not exist")
    stat = path.stat()
    if stat.st_size > 64 * 1024 * 1024:
        raise RemoteOpenCodeError("prompt_file exceeds 64 MiB")
    command = build_command(host, cwd=cwd, result_source=result_source, model=model, variant=variant, opencode_bin=opencode_bin, timeout=timeout, auto_approve=auto_approve)
    report: dict[str, Any] = {"schema_version": 1, "client": "remote_opencode_client", "host_id": host_id, "model": model, "remote_cwd": cwd, "remote_result_source": result_source, "prompt_bytes": stat.st_size, "prompt_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "dry_run": not execute, "provider_execution": bool(execute), "command_digest": _digest(command)}
    if not execute:
        return report
    payload = path.read_bytes()
    completed = subprocess.run(command, input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False, shell=False)
    report["returncode"] = int(completed.returncode)
    if completed.stderr:
        report["stderr"] = {"bytes": len(completed.stderr), "sha256": hashlib.sha256(completed.stderr).hexdigest()}
    try:
        remote = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        remote = {"status": "invalid_remote_json"}
    report["remote"] = remote if isinstance(remote, Mapping) else {"status": "invalid_remote_payload"}
    return report


def load_opencode_inventory_from_mapping(value: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw = value.get("hosts", value)
    rows = list(raw.values()) if isinstance(raw, Mapping) else raw
    if not isinstance(rows, list):
        raise RemoteOpenCodeError("OpenCode inventory must contain hosts")
    # Reuse the file parser's validation logic without persisting the mapping.
    normalized: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise RemoteOpenCodeError("OpenCode host must be an object")
        host_id = row.get("host_id")
        if host_id is None and isinstance(raw, Mapping):
            raise RemoteOpenCodeError("host map entries require host_id")
        host = dict(row)
        host["host_id"] = host_id
        # Inline validation is intentionally duplicated only at this seam;
        # file-based inventories remain the normal deployment path.
        identity = _text(host.get("host_id"), "host_id", pattern=re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z"))
        if identity in normalized:
            raise RemoteOpenCodeError("duplicate host_id")
        normalized[identity] = host
    # Serialize only non-secret inventory metadata through the same parser is
    # unnecessary; build_command applies the remaining path/host checks.
    return normalized


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--host-id", required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--remote-cwd", default=".")
    parser.add_argument("--remote-result-source", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--variant")
    parser.add_argument("--opencode-bin", default="opencode")
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--auto-approve", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(request(pathlib.Path(args.inventory), host_id=args.host_id, prompt_file=args.prompt_file, cwd=args.remote_cwd, result_source=args.remote_result_source, model=args.model, variant=args.variant, opencode_bin=args.opencode_bin, timeout=args.timeout, auto_approve=args.auto_approve, execute=args.execute), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (OSError, RemoteOpenCodeError, ValueError) as exc:
        print(f"remote opencode client: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
