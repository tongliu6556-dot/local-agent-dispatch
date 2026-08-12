from __future__ import annotations

import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import compute_resource_probe as probe  # noqa: E402


class ComputeResourceProbeTests(unittest.TestCase):
    def test_default_timeout_allows_slow_read_only_mount_inventory(self):
        args = probe.parse_args(["--inventory", "-"])
        self.assertEqual(20.0, args.timeout)

    def test_parse_output_keeps_project_compatibility_and_all_storage_mounts(self):
        payload = probe.parse_output(
            "\n".join(
                [
                    "META|container|Linux|x86_64",
                    "DISK|/workspace|1|590558003200|181999000000|1",
                    "DISK|/data|1|15360000000000|6000000000000|0",
                    "GPU|0,RTX 5090,32768,30000,10,595.0",
                ]
            )
        )
        self.assertEqual(2, len(payload["disks"]))
        self.assertEqual("/workspace", payload["best_writable_storage_path"])
        self.assertEqual("/data", payload["best_storage_path"])
        self.assertGreater(payload["disk_free_gib"], 169.0)
        self.assertTrue(payload["project_path_writable"])

    def test_parse_output_without_disk_is_explicitly_unknown_not_root_capacity(self):
        payload = probe.parse_output("META|container|Linux|x86_64\n")
        self.assertFalse(payload["project_path_exists"])
        self.assertEqual(0.0, payload["disk_free_gib"])
        self.assertIsNone(payload["best_writable_storage_path"])


if __name__ == "__main__":
    unittest.main()
