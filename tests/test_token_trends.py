"""Exercise the actual frontend data controller with synthetic requests."""
from pathlib import Path
import json
import shutil
import subprocess
import unittest


class TokenTrendContracts(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for frontend contracts")
    def test_data_sources_paging_cancellation_and_bounded_backfill(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([shutil.which("node"), "tests/token_trends.cjs"], cwd=root,
                                capture_output=True, text=True, encoding="utf8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["passed"], 22)
