import json
from pathlib import Path
import shutil
import subprocess
import unittest

from packet_inspector.server import MAX_STORAGE_BYTES, MIN_LEGACY_STORAGE_BYTES, MIN_STORAGE_BYTES, storage_settings


class StorageSizeUnitsTests(unittest.TestCase):
    def test_default_quota_is_exactly_500_decimal_megabytes(self):
        result = storage_settings({})
        self.assertEqual(result["max_storage_bytes"], 500_000_000)
        self.assertEqual(storage_settings(result), result)

    def test_legacy_limits_round_up_to_whole_megabytes(self):
        for legacy, expected in ((16, MIN_STORAGE_BYTES), (512, 537_000_000), (8192, MAX_STORAGE_BYTES)):
            with self.subTest(legacy=legacy):
                result = storage_settings({"max_storage_mb": legacy, "retention_policy": "records_first", "other": "preserved"})
                self.assertEqual(result["max_storage_bytes"], expected)
                self.assertGreaterEqual(expected, legacy * 1048576)
                self.assertLessEqual(result["max_storage_mb"], 8192)
                self.assertEqual(storage_settings(result), result)
                self.assertEqual(result["other"], "preserved")

    def test_saved_byte_limits_round_up_once(self):
        for size, expected in ((16_777_216, MIN_STORAGE_BYTES), (512_000_000, 512_000_000),
                               (512_000_123, 513_000_000), (8_589_934_592, MAX_STORAGE_BYTES),
                               (MAX_STORAGE_BYTES, MAX_STORAGE_BYTES)):
            with self.subTest(size=size):
                result = storage_settings({"max_storage_bytes": size})
                self.assertEqual(result["max_storage_bytes"], expected)
                self.assertEqual(storage_settings(result), result)
                self.assertGreaterEqual(result["max_storage_mb"] * 1048576, min(size, 8192 * 1048576))

    def test_changes_saved_by_older_clients_replace_a_stale_byte_value(self):
        result = storage_settings({"max_storage_mb": 32, "max_storage_bytes": 512000000})
        self.assertEqual(result["max_storage_bytes"], 34_000_000)

    def test_invalid_byte_settings_fall_back_to_the_legacy_budget(self):
        for size in (None, True, "512000000", 512000000.0, MIN_LEGACY_STORAGE_BYTES - 1, MAX_STORAGE_BYTES + 1):
            with self.subTest(size=size):
                self.assertEqual(storage_settings({"max_storage_mb": 32, "max_storage_bytes": size})["max_storage_bytes"],
                                 34_000_000)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for frontend contracts")
    def test_frontend_decimal_formatting_and_settings_round_trips(self):
        result = subprocess.run([shutil.which("node"), "tests/decimal_size_units.cjs"],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                text=True, encoding="utf8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["passed"])
