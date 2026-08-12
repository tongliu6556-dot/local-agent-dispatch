from __future__ import annotations

import json
import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import server_local_model_scan as scan  # noqa: E402


class ServerLocalModelScanTests(unittest.TestCase):
    def test_remote_scanner_has_no_private_fixed_worktree_or_raw_process_args(self):
        self.assertNotIn("/private-server-root", scan.REMOTE_SCANNER)
        self.assertNotIn("ps -eo pid=,args=", scan.REMOTE_SCANNER)
        self.assertIn("LAD_PROBE_PROJECT", scan.REMOTE_SCANNER)

    def test_server_recipes_use_injected_paths_and_route_identity(self):
        for name in (
            "deploy_server_local_qwen25_awq.sh",
            "server_local_agentic_smoke.sh",
            "wait_for_server_local_smoke.sh",
            "codex_large_download_guard.sh",
        ):
            text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            self.assertNotIn("/private-server-root", text, name)
            self.assertNotIn("/private-server-bin", text, name)
        deploy = (ROOT / "scripts" / "deploy_server_local_qwen25_awq.sh").read_text(encoding="utf-8")
        self.assertIn("LAD_EXPECTED_EGRESS", deploy)

    def test_declared_project_path_is_passed_to_local_probe(self):
        payload = {"runtime_commands": {}, "python_modules": [], "active_processes": [], "listeners": [], "apis": [], "model_directories": []}

        class Completed:
            returncode = 0
            stdout = json.dumps(payload)
            stderr = ""

        with mock.patch.object(scan.subprocess, "run", return_value=Completed()) as run:
            host_id, row = scan.scan_host(
                {"host_id": "local", "transport": "local", "project_path": "/srv/project"},
                2,
            )
        self.assertEqual("local", host_id)
        self.assertTrue(row["reachable"])
        script = run.call_args.kwargs["input"]
        self.assertIn("LAD_PROBE_PROJECT", script)
        self.assertIn("/srv/project", script)

    def test_project_path_rejects_control_characters(self):
        with self.assertRaises(ValueError):
            scan.scan_host({"host_id": "bad", "transport": "local", "project_path": "/srv/x\nrun"}, 2)


if __name__ == "__main__":
    unittest.main()
