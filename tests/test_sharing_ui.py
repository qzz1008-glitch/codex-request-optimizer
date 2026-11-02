from pathlib import Path
import shutil
import subprocess
import unittest


class SharingUIContracts(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for frontend contracts")
    def test_sharing_tab_memory_and_operation_fallback(self):
        result = subprocess.run([shutil.which("node"), "tests/sharing_views.cjs"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding="utf8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"sharing_views":"passed"', result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for frontend contracts")
    def test_sharing_actions_feedback_and_existing_view_selection(self):
        result = subprocess.run([shutil.which("node"), "tests/sharing_ui.cjs"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding="utf8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"sharing_ui":"passed"', result.stdout)
