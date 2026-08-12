import unittest
import sys
import pathlib

# Ensure scripts directory is in path
SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import dispatch_schema
from dispatch_schema import SchemaValidationError

class TestDispatchSchema(unittest.TestCase):
    def test_positive_fixtures(self):
        # Valid runtime_state
        state = {
            "schema_version": 1,
            "pools": {
                "antigravity.gemini": {"health": "ready"}
            }
        }
        dispatch_schema.validate("runtime_state", state)

        # Valid task_packet
        packet = {
            "schema_version": 1,
            "job": {"id": "123", "task_type": "code"},
            "attempt": {"id": "456", "pool": "codex.luna"}
        }
        dispatch_schema.validate("task_packet", packet)

        # Valid dispatch_plan
        plan = {
            "schema_version": 1,
            "assignments": [],
            "decision": "pause"
        }
        dispatch_schema.validate("dispatch_plan", plan)

        capture = {
            "schema_version": 1,
            "capture": "bounded-task-capture",
            "read_only": True,
            "provider_prompts_sent": False,
            "project_executed": False,
            "task_id": "task-1",
            "task_family": "analysis",
            "dag": {},
            "planner_jobs": [],
            "estimate": {},
            "unknown_semantics": {},
        }
        dispatch_schema.validate("task_capture", capture)
        
        # Valid event
        event = {
            "schema_version": 1,
            "type": "job_completed"
        }
        dispatch_schema.validate("event", event)

    def test_missing_schema_version(self):
        with self.assertRaisesRegex(SchemaValidationError, "Missing schema_version"):
            dispatch_schema.validate("event", {"type": "job_completed"})

    def test_unsupported_schema_version(self):
        with self.assertRaisesRegex(SchemaValidationError, "Unsupported schema_version: 2"):
            dispatch_schema.validate("event", {"schema_version": 2})

    def test_malformed_pool_field(self):
        # pools must be dict
        with self.assertRaisesRegex(SchemaValidationError, "malformed pools field: must be a dict"):
            dispatch_schema.validate("runtime_state", {"schema_version": 1, "pools": ["codex.luna"]})
        
        # pool entry must be dict
        with self.assertRaisesRegex(SchemaValidationError, "malformed pool entry for codex.luna: must be a dict"):
            dispatch_schema.validate("runtime_state", {"schema_version": 1, "pools": {"codex.luna": "ready"}})
            
        # pool must be dict if present at top level
        with self.assertRaisesRegex(SchemaValidationError, "malformed pool field: must be a dict"):
            dispatch_schema.validate("runtime_state", {"schema_version": 1, "pool": "codex.luna"})

    def test_malformed_job_field(self):
        with self.assertRaisesRegex(SchemaValidationError, "malformed job field: must be a dict"):
            dispatch_schema.validate("task_packet", {"schema_version": 1, "job": "job_123"})

    def test_malformed_attempt_field(self):
        with self.assertRaisesRegex(SchemaValidationError, "malformed attempt field: must be a dict"):
            dispatch_schema.validate("task_packet", {"schema_version": 1, "attempt": "attempt_123"})

    def test_secret_like_fields(self):
        # Top-level secret
        with self.assertRaisesRegex(SchemaValidationError, "Secret-like field detected in public snapshot: api_key"):
            dispatch_schema.validate("event", {"schema_version": 1, "api_key": "sk-12345"})

        # Nested secret
        with self.assertRaisesRegex(SchemaValidationError, "Secret-like field detected in public snapshot: my_PASSWORD"):
            dispatch_schema.validate("task_packet", {
                "schema_version": 1,
                "job": {
                    "env": {
                        "my_PASSWORD": "secret_value"
                    }
                }
            })
            
        # Inside lists
        with self.assertRaisesRegex(SchemaValidationError, "Secret-like field detected in public snapshot: token"):
            dispatch_schema.validate("dispatch_plan", {
                "schema_version": 1,
                "assignments": [
                    {"token": "ghp_123"}
                ]
            })

if __name__ == "__main__":
    unittest.main()
