from __future__ import annotations

import importlib.util
import json
import pathlib
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "resource_governor.py"
SPEC = importlib.util.spec_from_file_location("resource_governor", SCRIPT)
assert SPEC and SPEC.loader
GOV = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GOV)


class ResourceGovernorTests(unittest.TestCase):
    def test_process_parser_drops_prompt_and_keeps_mcp_process(self) -> None:
        rows = GOV.parse_unix_processes(
            "101 2097152 439000000 120.0 /opt/homebrew/bin/codebase-memory-mcp --prompt SECRET\n"
            "102 100000 400000000 2.0 /Applications/ChatGPT.app/codex --task PRIVATE\n",
            current_pid=999,
        )
        self.assertEqual(rows[0]["process_class"], "codebase_memory_mcp")
        serialized = json.dumps(rows)
        self.assertNotIn("SECRET", serialized)
        self.assertNotIn("PRIVATE", serialized)
        self.assertNotIn("--prompt", serialized)
        self.assertEqual(rows[1]["process_class"], "codex")

    def test_high_swap_enters_conserve_even_when_pressure_text_is_normal(self) -> None:
        self.assertEqual(
            GOV.pressure_tier(
                total_bytes=32 * GOV.GIB,
                available_bytes=8 * GOV.GIB,
                swap_total_bytes=2304 * GOV.MIB,
                swap_used_bytes=2010 * GOV.MIB,
                pressure_state="normal",
            ),
            "conserve",
        )

    def test_conserve_blocks_new_lanes_and_routes_remote(self) -> None:
        report = GOV.build_report(
            ram={
                "total_bytes": 32 * GOV.GIB,
                "available_bytes": 8 * GOV.GIB,
                "swap_total_bytes": 2304 * GOV.MIB,
                "swap_used_bytes": 2010 * GOV.MIB,
                "pressure_state": "normal",
            },
            processes=[
                {"pid": 1, "process_class": "codebase_memory_mcp", "rss_bytes": 2 * GOV.GIB},
            ],
            requested_lanes=5,
            per_lane_peak_bytes=1536 * GOV.MIB,
            max_local_lanes=5,
        )
        self.assertEqual(report["ram"]["pressure_tier"], "conserve")
        self.assertFalse(report["admission"]["local_agent_launch_allowed"])
        self.assertEqual(report["admission"]["max_new_local_lanes"], 0)
        self.assertEqual(report["admission"]["decision"], "throttle")
        self.assertTrue(any(a["action"] == "route_compatible_work_remote" for a in report["actions"]))
        self.assertFalse(report["safety"]["automatic_kill"])

    def test_critical_only_lists_owned_pids_for_pause(self) -> None:
        report = GOV.build_report(
            ram={
                "total_bytes": 32 * GOV.GIB,
                "available_bytes": 2 * GOV.GIB,
                "swap_total_bytes": 2 * GOV.GIB,
                "swap_used_bytes": 2 * GOV.GIB,
                "pressure_state": "critical",
            },
            processes=[
                {"pid": 10, "process_class": "opencode", "rss_bytes": GOV.GIB},
                {"pid": 11, "process_class": "codex", "rss_bytes": GOV.GIB},
            ],
            requested_lanes=1,
            per_lane_peak_bytes=GOV.GIB,
            max_local_lanes=2,
            owned_pids=[10],
        )
        self.assertEqual(report["ram"]["pressure_tier"], "emergency")
        pause = next(a for a in report["actions"] if a["action"] == "pause_owned_lanes")
        self.assertEqual(pause["pids"], [10])
        self.assertTrue(any(a["action"] == "do_not_kill_unowned_processes" for a in report["actions"]))

    def test_normal_headroom_computes_bounded_lane_capacity(self) -> None:
        report = GOV.build_report(
            ram={
                "total_bytes": 32 * GOV.GIB,
                "available_bytes": 16 * GOV.GIB,
                "swap_total_bytes": 2 * GOV.GIB,
                "swap_used_bytes": 0,
                "pressure_state": "normal",
            },
            processes=[],
            requested_lanes=2,
            per_lane_peak_bytes=2 * GOV.GIB,
            max_local_lanes=5,
        )
        self.assertEqual(report["ram"]["pressure_tier"], "normal")
        self.assertEqual(report["admission"]["max_new_local_lanes"], 4)
        self.assertEqual(report["admission"]["decision"], "admit")


if __name__ == "__main__":
    unittest.main()
