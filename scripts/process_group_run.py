"""Bounded process-group execution helper.

Starts a child process (and any grandchildren) in a dedicated process group
so the entire tree can be signalled on timeout.  On POSIX the child gets its
own session via ``os.setsid``; on Windows a new process group is created via
``CREATE_NEW_PROCESS_GROUP`` and cleanup falls back to ``taskkill /T /F``.
"""

from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import sys
from dataclasses import dataclass

_IS_WINDOWS = sys.platform == "win32"

_GRACE_PERIOD_SECONDS = 3
_TIMEOUT_RETURNCODE = 124


@dataclass(frozen=True, slots=True)
class ProcessGroupResult:
    """Result of a process-group execution.

    Attributes:
        stdout: Combined stdout and stderr of the child process.
        returncode: Exit code of the child, or 124 if the process timed out.
        timed_out: ``True`` when the process was killed due to timeout.
    """

    stdout: str
    returncode: int
    timed_out: bool


def _kill_process_group_posix(proc: subprocess.Popen[bytes]) -> None:
    """Send SIGTERM then SIGKILL to the child's process group (POSIX)."""
    pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        # Process (group) already dead – nothing to do.
        return

    try:
        proc.wait(timeout=_GRACE_PERIOD_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass


def _kill_process_group_windows(proc: subprocess.Popen[bytes]) -> None:
    """Best-effort recursive kill via ``taskkill`` (Windows)."""
    try:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_GRACE_PERIOD_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def run_in_process_group(
    argv: list[str],
    *,
    cwd: str | None = None,
    stdin_data: str | None = None,
    timeout_seconds: int = 3600,
    pid_path: str | None = None,
) -> ProcessGroupResult:
    """Run *argv* in a new process group with an optional timeout.

    Parameters:
        argv: Command and arguments to execute.
        cwd: Working directory for the child process.
        stdin_data: Optional string piped to the child's stdin.
        timeout_seconds: Maximum wall-clock seconds before the process group
            is terminated.  Defaults to one hour.
        pid_path: Optional controller-confined breadcrumb containing the live
            child PID.  It is removed when the process exits.

    Returns:
        A :class:`ProcessGroupResult` containing captured output, exit code,
        and whether the execution timed out.
    """
    popen_kwargs: dict[str, object] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
    }

    if cwd is not None:
        popen_kwargs["cwd"] = cwd

    if stdin_data is not None:
        popen_kwargs["stdin"] = subprocess.PIPE

    if _IS_WINDOWS:
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        # `start_new_session` is the thread-safe subprocess equivalent of
        # `setsid`; avoid `preexec_fn` because the controller may later gain
        # parallel lanes and Python warns against running arbitrary code after
        # fork in a multi-threaded process.
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(argv, **popen_kwargs)  # type: ignore[call-overload]
    pid_file = pathlib.Path(pid_path).expanduser() if pid_path else None
    try:
        if pid_file is not None:
            # The controller supplies this path only after workspace
            # confinement. Keep the runner generic but write just the child
            # PID; no argv or prompt content is persisted.
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text(f"{proc.pid}\n", encoding="utf-8")
        encoded_input = stdin_data.encode() if stdin_data is not None else None
        raw_output, _ = proc.communicate(
            input=encoded_input,
            timeout=timeout_seconds,
        )
        return ProcessGroupResult(
            stdout=raw_output.decode(errors="replace"),
            returncode=proc.returncode,
            timed_out=False,
        )
    except subprocess.TimeoutExpired as exc:
        # Capture any partial output attached to the exception.
        partial = exc.stdout or b""

        # Kill the entire process group.
        if _IS_WINDOWS:
            _kill_process_group_windows(proc)
        else:
            _kill_process_group_posix(proc)

        # Drain whatever remains in the pipe after killing.
        try:
            remaining, _ = proc.communicate(timeout=_GRACE_PERIOD_SECONDS)
        except subprocess.TimeoutExpired:
            remaining = b""

        combined = partial + remaining
        return ProcessGroupResult(
            stdout=combined.decode(errors="replace"),
            returncode=_TIMEOUT_RETURNCODE,
            timed_out=True,
        )
    finally:
        if pid_file is not None:
            try:
                pid_file.unlink()
            except FileNotFoundError:
                pass
        # Belt-and-suspenders: make sure the child is truly gone.
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=_GRACE_PERIOD_SECONDS)
            except subprocess.TimeoutExpired:
                pass
