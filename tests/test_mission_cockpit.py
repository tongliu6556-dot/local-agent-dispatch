from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import mission_cockpit  # noqa: E402


class MissionCockpitTests(unittest.TestCase):
    def test_l0_exposes_gate_risk_and_safe_decision_without_prompt(self) -> None:
        report = mission_cockpit.build_cockpit(
            {
                "jobs": [{"job_id": "j1", "status": "running", "model": "spark", "pool_id": "codex.spark"}],
                "workers": [{"job_id": "j1", "status": "running", "model": "spark", "execution_host": "local", "workload_host": "remote"}],
            },
            mission={"mission_id": "m1", "goal": {"value": "bounded work"}, "claim_envelope": {"forbidden": ["full claim"]}},
            governor={"ram": {"pressure_tier": "conserve", "available_bytes": 10}, "admission": {"decision": "throttle", "max_new_local_lanes": 0}, "observed_at_utc": "now"},
        )
        self.assertEqual("execution_and_validation", report["current_gate"])
        self.assertEqual("local_memory_pressure", report["risks"][0]["kind"])
        self.assertEqual("keep_new_local_lanes_blocked", report["decision_required"]["safe_default"])
        self.assertEqual("remote", report["active_assignments"][0]["workload_host"])
        self.assertFalse(report["sources"]["raw_prompt_persisted"])


if __name__ == "__main__":
    unittest.main()
