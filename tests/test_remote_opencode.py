from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import remote_opencode_client as client  # noqa: E402


class RemoteOpenCodeTests(unittest.TestCase):
    def test_remote_runner_reads_stdin_and_publishes_only_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            fake = root / "fake-opencode.py"
            capture = root / "argv.json"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "path=os.environ['CAPTURE']\n"
                "pathlib=None\n"
                "open(path, 'w').write(json.dumps(sys.argv[1:]))\n"
                "print(json.dumps({'type':'text','part':{'text':'remote answer'}}))\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            env = dict(os.environ, CAPTURE=str(capture))
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "opencode_remote_run.py"),
                    "--cwd",
                    str(root),
                    "--model",
                    "opencode-go/deepseek-v4-flash",
                    "--result-source",
                    "out/result.md",
                    "--opencode-bin",
                    str(fake),
                ],
                input="private task text",
                text=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual("completed", summary["status"])
            self.assertEqual("remote answer\n", (root / "out/result.md").read_text())
            self.assertNotIn("private task text", capture.read_text())

    def test_remote_runner_rejects_result_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "opencode_remote_run.py"),
                    "--cwd",
                    str(root),
                    "--model",
                    "opencode-go/deepseek-v4-flash",
                    "--result-source",
                    "../outside.txt",
                ],
                input="x",
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(2, result.returncode)
            self.assertIn("escapes cwd", result.stderr)

    def test_client_dry_run_does_not_open_ssh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            prompt = root / "task.md"
            prompt.write_text("small task", encoding="utf-8")
            inventory = {
                "hosts": [
                    {
                        "host_id": "remote-b",
                        "transport": "ssh",
                        "hostname": "remote-b.example.test",
                        "user": "runner",
                        "port": 2223,
                        "project_path": "/srv/local-agent-dispatch",
                        "opencode_runner": "/srv/local-agent-dispatch/scripts/opencode_remote_run.py",
                        "opencode_bin": "/srv/local-agent-dispatch/.opencode/bin/opencode",
                    }
                ]
            }
            report = client.request(
                inventory,
                host_id="remote-b",
                prompt_file=prompt,
                cwd="remote-workspace",
                result_source="out/result.md",
                model="opencode-go/deepseek-v4-flash",
            )
            self.assertTrue(report["dry_run"])
            self.assertFalse(report["provider_execution"])
            self.assertEqual(len("small task"), report["prompt_bytes"])
            self.assertIn("command_digest", report)

    def test_client_rejects_non_go_model(self) -> None:
        with self.assertRaises(client.RemoteOpenCodeError):
            client.build_command(
                {
                    "hostname": "remote-b.example.test",
                    "user": "runner",
                    "port": 2223,
                    "project_path": "/srv/project",
                    "opencode_runner": "/srv/project/scripts/opencode_remote_run.py",
                },
                cwd=".",
                result_source="out/result.md",
                model="gpt-5",
                variant=None,
                opencode_bin="opencode",
                timeout=60,
                auto_approve=False,
            )


if __name__ == "__main__":
    unittest.main()
