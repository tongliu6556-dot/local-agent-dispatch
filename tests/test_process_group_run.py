from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import process_group_run  # noqa: E402


class ProcessGroupRunTests(unittest.TestCase):
    def test_combines_stdout_and_stderr(self) -> None:
        result = process_group_run.run_in_process_group(
            [
                sys.executable,
                "-c",
                "import sys; print('stdout-line'); print('stderr-line', file=sys.stderr)",
            ],
            timeout_seconds=5,
        )
        self.assertFalse(result.timed_out)
        self.assertEqual(0, result.returncode)
        self.assertIn("stdout-line", result.stdout)
        self.assertIn("stderr-line", result.stdout)

    def test_pid_breadcrumb_is_live_only_and_removed_after_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_path = pathlib.Path(tmp) / "worker.pid"
            result = process_group_run.run_in_process_group(
                [sys.executable, "-c", "print('ok')"],
                timeout_seconds=5,
                pid_path=str(pid_path),
            )
            self.assertEqual(0, result.returncode)
            self.assertFalse(pid_path.exists())

    @unittest.skipIf(sys.platform == "win32", "POSIX process-group assertion")
    def test_timeout_terminates_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = pathlib.Path(tmp) / "grandchild-finished"
            grandchild_code = (
                "import pathlib, time; time.sleep(3); "
                "pathlib.Path(%r).write_text('finished')" % str(marker)
            )
            child_code = (
                "import subprocess, sys, time; "
                "subprocess.Popen([sys.executable, '-c', %r]); time.sleep(10)" % grandchild_code
            )
            result = process_group_run.run_in_process_group(
                [sys.executable, "-c", child_code],
                timeout_seconds=1,
            )
            self.assertTrue(result.timed_out)
            self.assertEqual(124, result.returncode)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
