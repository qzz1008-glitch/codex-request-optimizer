import json
from pathlib import Path
import shutil
import subprocess
import unittest

from packet_inspector.server import MAX_STORAGE_BYTES, MIN_STORAGE_BYTES, storage_settings


class StorageSizeUnitsTests(unittest.TestCase):
    def test_legacy_limits_keep_the_original_byte_budget(self):
        for legacy in (16, 512, 8192):
            with self.subTest(legacy=legacy):
                result = storage_settings({"max_storage_mb": legacy, "other": "preserved"})
                self.assertEqual(result["max_storage_bytes"], legacy * 1048576)
                self.assertEqual(result["max_storage_mb"], legacy)
                self.assertEqual(storage_settings(result), result)
                self.assertEqual(result["other"], "preserved")

    def test_exact_byte_limits_round_trip_with_safe_legacy_aliases(self):
        for size in (MIN_STORAGE_BYTES, 512000000, 512000123, MAX_STORAGE_BYTES):
            with self.subTest(size=size):
                result = storage_settings({"max_storage_bytes": size})
                self.assertEqual(result["max_storage_bytes"], size)
                self.assertEqual(storage_settings(result), result)
                self.assertGreaterEqual(result["max_storage_mb"] * 1048576, size)

    def test_changes_saved_by_older_clients_replace_a_stale_byte_value(self):
        result = storage_settings({"max_storage_mb": 32, "max_storage_bytes": 512000000})
        self.assertEqual(result["max_storage_bytes"], 32 * 1048576)

    def test_invalid_byte_settings_fall_back_to_the_legacy_budget(self):
        for size in (None, True, "512000000", 512000000.0, MIN_STORAGE_BYTES - 1, MAX_STORAGE_BYTES + 1):
            with self.subTest(size=size):
                self.assertEqual(storage_settings({"max_storage_mb": 32, "max_storage_bytes": size})["max_storage_bytes"],
                                 32 * 1048576)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for frontend contracts")
    def test_frontend_decimal_formatting_and_settings_round_trips(self):
        result = subprocess.run([shutil.which("node"), "tests/decimal_size_units.cjs"],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                text=True, encoding="utf8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["passed"])
