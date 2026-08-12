from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import legacy_history  # noqa: E402
from sqlite_store import SQLiteStore  # noqa: E402


class LegacyHistoryTests(unittest.TestCase):
    def test_summary_is_metadata_only_and_marks_legacy_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "run-a"
            root.mkdir()
            (root / "state.json").write_text(
                json.dumps({
                    "run_id": "run-a",
                    "status": "running",
                    "pid": 99999999,
                    "jobs": [{
                        "job_id": "job-a",
                        "status": "queued",
                        "model": "gpt-5.3-codex-spark",
                        "prompt": "DO NOT PERSIST",
                        "attempts": [{"adapter": "command", "argv": ["secret"]}],
                    }],
                }),
                encoding="utf-8",
            )
            (root / "events.jsonl").write_text('{"event":"job_started"}\nnot-json\n', encoding="utf-8")
            report = legacy_history.summarize_run(root, reconcile=True, liveness_probe=lambda _pid: "dead")
            encoded = json.dumps(report)
            self.assertNotIn("DO NOT PERSIST", encoded)
            self.assertNotIn("secret", encoded)
            self.assertEqual("legacy_incomplete", report["evidence_quality"])
            self.assertEqual(1, report["events"]["malformed_lines"])
            self.assertEqual("dead", report["liveness"]["job-a"])

    def test_import_writes_only_sanitized_job_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            report = {
                "run_id": "run-a",
                "run_dir": "/private/run-a",
                "jobs": [{
                    "job_id": "job-a",
                    "status": "completed",
                    "model": "spark",
                    "adapter": "command",
                    "prompt": "secret must not be imported",
                }],
            }
            db = root / "import.sqlite3"
            result = legacy_history.import_to_sqlite([report], db)
            self.assertEqual(1, result["imported_jobs"])
            with SQLiteStore(db) as store:
                payload = store.list_jobs()[0]["payload"]
                self.assertNotIn("prompt", payload)
                self.assertEqual("legacy_incomplete", payload["legacy_evidence_quality"])
                self.assertEqual("completed", store.list_jobs()[0]["status"])


if __name__ == "__main__":
    unittest.main()
