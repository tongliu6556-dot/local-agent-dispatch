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

import remote_worker_client as client  # noqa: E402


def _packet(root: pathlib.Path) -> dict:
    return {
        "schema_version": 1,
        "packet_id": "packet-transport",
        "job_id": "job-transport",
        "workspace": str(root),
        "execution_host": "remote-a",
        "workload_host": "remote-b",
        "workload_wrapper": "declared-wrapper",
        "write_scope": "src",
        "required_artifacts": ["out/result.json"],
        "validation_required": True,
        "validation_argv": ["python3", "-m", "unittest"],
        "api_key": "must-never-cross-the-redaction-boundary",
        "attempts": [
            {
                "attempt_id": "attempt-transport",
                "adapter": "server_local",
                "transport": "ssh",
                "model": "local/fake",
                "execution_host": "remote-a",
                "workload_host": "remote-b",
                "prompt_file": "TASK.md",
                "prompt": "private prompt must not be sent as a field",
                "argv": ["provider", "--prompt", "private prompt"],
            }
        ],
    }


def _inventory(root: pathlib.Path, script: pathlib.Path) -> dict:
    return {
        "hosts": [
            {
                "host_id": "remote-a",
                "transport": "ssh",
                "hostname": "remote-a.example.test",
                "user": "runner",
                "port": 2222,
                "worker_script": str(script),
                "project_path": str(root),
                "spool_path": str(root / "spool"),
            },
            {
                "host_id": "remote-b",
                "transport": "ssh",
                "hostname": "remote-b.example.test",
                "user": "runner",
                "port": 2223,
                "worker_script": str(script),
                "project_path": str(root),
                "spool_path": str(root / "spool-remote-b"),
            },
        ]
    }


def _fake_ssh(path: pathlib.Path) -> None:
    # This fake has the same pipe shape as ssh: it receives a target and a
    # remote argv, then executes the fixed local worker script without a shell.
    path.write_text(
        """#!/usr/bin/env python3
import json
import os
import subprocess
import sys

args = sys.argv[1:]
if os.environ.get('FAKE_SSH_ARGS'):
    with open(os.environ['FAKE_SSH_ARGS'], 'w', encoding='utf-8') as handle:
        json.dump(args, handle)
data = sys.stdin.buffer.read()
if os.environ.get('FAKE_SSH_CAPTURE'):
    with open(os.environ['FAKE_SSH_CAPTURE'], 'ab') as handle:
        handle.write(data)
if os.environ.get('FAKE_SSH_FAIL'):
    sys.stderr.write('private prompt argv token should not be returned\\n')
    raise SystemExit(19)
try:
    index = args.index('python3')
except ValueError:
    sys.stderr.write('missing fixed python3 command\\n')
    raise SystemExit(18)
completed = subprocess.run(
    [sys.executable, *args[index + 1:]],
    input=data,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
)
sys.stdout.buffer.write(completed.stdout)
sys.stderr.buffer.write(completed.stderr)
raise SystemExit(completed.returncode)
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class RemoteWorkerClientTests(unittest.TestCase):
    def test_dry_run_is_default_and_packet_stays_on_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "src").mkdir()
            fake = root / "ssh-fake.py"
            _fake_ssh(fake)
            inventory = _inventory(root, ROOT / "scripts" / "remote_worker.py")
            packet = _packet(root)
            capture = root / "captured-input"
            args_path = root / "ssh-args.json"
            old_capture = os.environ.get("FAKE_SSH_CAPTURE")
            old_args = os.environ.get("FAKE_SSH_ARGS")
            os.environ["FAKE_SSH_CAPTURE"] = str(capture)
            os.environ["FAKE_SSH_ARGS"] = str(args_path)
            try:
                transport = client.RemoteWorkerClient(inventory, ssh_executable=str(fake))
                dry = transport.prepare(host_id="remote-a", packet=packet)
                self.assertTrue(dry["dry_run"])
                self.assertFalse(capture.exists())
                self.assertEqual("remote-a", dry["execution_host"])
                self.assertEqual("remote-b", dry["workload_host"])
                self.assertTrue(dry["placement"]["split_placement"])
                self.assertEqual(3, dry["redactions_applied"])

                prepared = transport.prepare(host_id="remote-a", packet=packet, execute=True)
                self.assertFalse(prepared["dry_run"])
                self.assertEqual("prepared", prepared["remote"]["status"])
                sent = json.loads(capture.read_text(encoding="utf-8"))
                sent_text = json.dumps(sent, ensure_ascii=False)
                self.assertEqual("packet-transport", sent["packet_id"])
                self.assertNotIn("private prompt", sent_text)
                self.assertNotIn('"argv":', sent_text)
                self.assertNotIn("--prompt", sent_text)
                self.assertNotIn("api_key", sent_text)

                ssh_args = json.loads(args_path.read_text(encoding="utf-8"))
                self.assertNotIn("sh", ssh_args)
                self.assertNotIn("-c", ssh_args)
                self.assertIn("python3", ssh_args)
                self.assertIn("-", ssh_args)
                # The packet is not interpolated into any SSH argument.
                self.assertNotIn("private prompt", json.dumps(ssh_args))
            finally:
                if old_capture is None:
                    os.environ.pop("FAKE_SSH_CAPTURE", None)
                else:
                    os.environ["FAKE_SSH_CAPTURE"] = old_capture
                if old_args is None:
                    os.environ.pop("FAKE_SSH_ARGS", None)
                else:
                    os.environ["FAKE_SSH_ARGS"] = old_args

    def test_prepare_maps_local_absolute_paths_into_remote_project_root(self) -> None:
        """A real SSH target must not receive the Mac workspace path."""
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as remote_tmp:
            root = pathlib.Path(tmp)
            remote_root = pathlib.Path(remote_tmp)
            (root / "src").mkdir()
            (remote_root / "src").mkdir()
            fake = root / "ssh-fake.py"
            _fake_ssh(fake)
            inventory = _inventory(root, ROOT / "scripts" / "remote_worker.py")
            for host in inventory["hosts"]:
                host["project_path"] = str(remote_root)
                host["spool_path"] = str(remote_root / "spool" / host["host_id"])
            capture = root / "mapped-packet.json"
            old_capture = os.environ.get("FAKE_SSH_CAPTURE")
            os.environ["FAKE_SSH_CAPTURE"] = str(capture)
            try:
                transport = client.RemoteWorkerClient(inventory, ssh_executable=str(fake))
                prepared = transport.prepare(host_id="remote-a", packet=_packet(root), execute=True)
                self.assertEqual("prepared", prepared["remote"]["status"])
                sent = json.loads(capture.read_text(encoding="utf-8"))
                self.assertEqual(str(remote_root), sent["workspace"])
                self.assertNotEqual(str(root), sent["workspace"])
                self.assertNotIn(str(root), json.dumps(sent))
            finally:
                if old_capture is None:
                    os.environ.pop("FAKE_SSH_CAPTURE", None)
                else:
                    os.environ["FAKE_SSH_CAPTURE"] = old_capture

    def test_prepare_fake_execute_and_resume_handoff_preserve_split_placement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "src").mkdir()
            fake = root / "ssh-fake.py"
            _fake_ssh(fake)
            inventory = _inventory(root, ROOT / "scripts" / "remote_worker.py")
            packet = _packet(root)
            transport = client.RemoteWorkerClient(inventory, ssh_executable=str(fake))
            transport.prepare(host_id="remote-a", packet=packet, execute=True)
            result = transport.fake_execute(
                host_id="remote-a",
                job_id="job-transport",
                owner="continuation-worker",
                packet=packet,
                execute=True,
            )
            self.assertEqual("completed", result["remote"]["status"])
            self.assertEqual("remote-a", result["execution_host"])
            self.assertEqual("remote-b", result["workload_host"])
            handoff_path = root / "handoff.json"
            handoff = transport.handoff(
                host_id="remote-a",
                job_id="job-transport",
                packet=packet,
                execute=True,
                output=handoff_path,
            )
            self.assertEqual("completed", handoff["remote"]["status"])
            self.assertFalse(handoff["remote"]["resume_allowed"])
            self.assertEqual("review_artifacts", handoff["remote"]["next_action"])
            self.assertTrue(handoff_path.is_file())
            handoff_text = handoff_path.read_text(encoding="utf-8")
            self.assertNotIn("private prompt", handoff_text)
            self.assertNotIn("argv", handoff_text)
            self.assertNotIn("lease_token", handoff_text)
            resumed = transport.resume(host_id="remote-a", job_id="job-transport", execute=True)
            self.assertEqual("completed", resumed["remote"]["status"])
            self.assertFalse(resumed["remote"]["recovery"]["performed"])

    def test_fake_service_transport_is_explicit_and_provider_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "src").mkdir()
            fake = root / "ssh-fake.py"
            _fake_ssh(fake)
            inventory = _inventory(root, ROOT / "scripts" / "remote_worker.py")
            packet = _packet(root)
            transport = client.RemoteWorkerClient(inventory, ssh_executable=str(fake))

            dry = transport.fake_service(
                host_id="remote-a",
                job_id="job-transport",
                owner="server-service",
            )
            self.assertTrue(dry["dry_run"])
            self.assertFalse(dry["executed"])
            self.assertEqual("fake-service", dry["operation"])
            self.assertTrue(dry["chat_independent"])
            self.assertFalse(dry["provider_execution"])
            self.assertEqual("provider_free_fixture", dry["service_boundary"])

            transport.prepare(host_id="remote-a", packet=packet, execute=True)
            result = transport.fake_service(
                host_id="remote-a",
                job_id="job-transport",
                owner="server-service",
                packet=packet,
                poll_seconds=0.01,
                max_idle_rounds=3,
                execute=True,
            )
            self.assertEqual("completed", result["remote"]["status"])
            self.assertFalse(result["remote"]["provider_execution"])
            self.assertEqual("remote-a", result["execution_host"])
            self.assertEqual("remote-b", result["workload_host"])

    def test_status_recover_and_handoff_are_supported_without_packet_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "src").mkdir()
            fake = root / "ssh-fake.py"
            _fake_ssh(fake)
            transport = client.RemoteWorkerClient(
                _inventory(root, ROOT / "scripts" / "remote_worker.py"),
                ssh_executable=str(fake),
            )
            transport.prepare(host_id="remote-a", packet=_packet(root), execute=True)
            status = transport.status(host_id="remote-a", job_id="job-transport", execute=True)
            self.assertEqual(1, len(status["remote"]["jobs"]))
            recovered = transport.recover(host_id="remote-a", job_id="job-transport", execute=True)
            self.assertEqual("prepared", recovered["remote"]["jobs"][0]["status"])
            # The transport still opens stdin as a pipe, but only sends an
            # empty payload for operations that do not consume a task packet.
            self.assertEqual("empty", status["stdin_transport"])

    def test_remote_stderr_is_reduced_to_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            fake = root / "ssh-fake.py"
            _fake_ssh(fake)
            transport = client.RemoteWorkerClient(
                _inventory(root, ROOT / "scripts" / "remote_worker.py"),
                ssh_executable=str(fake),
            )
            old = os.environ.get("FAKE_SSH_FAIL")
            os.environ["FAKE_SSH_FAIL"] = "1"
            try:
                with self.assertRaises(client.ClientError) as raised:
                    transport.status(host_id="remote-a", job_id="job-transport", execute=True)
            finally:
                if old is None:
                    os.environ.pop("FAKE_SSH_FAIL", None)
                else:
                    os.environ["FAKE_SSH_FAIL"] = old
            message = str(raised.exception)
            self.assertNotIn("private prompt", message)
            self.assertNotIn("argv", message)
            self.assertNotIn("token should", message)
            self.assertIn("sha256", message)

    def test_inventory_rejects_unsafe_port_and_remote_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            script = ROOT / "scripts" / "remote_worker.py"
            bad_port = _inventory(root, script)
            bad_port["hosts"][0]["port"] = 70000
            with self.assertRaises(client.ClientError):
                client.RemoteWorkerClient(bad_port)
            bad_path = _inventory(root, script)
            bad_path["hosts"][0]["spool_path"] = "/srv/lad/../escape"
            with self.assertRaises(client.ClientError):
                client.RemoteWorkerClient(bad_path)
            local = _inventory(root, script)
            local["hosts"][0]["transport"] = "local"
            with self.assertRaises(client.ClientError):
                client.RemoteWorkerClient(local)


if __name__ == "__main__":
    unittest.main()
