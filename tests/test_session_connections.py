"""Check sidebar links against confirmed subagent relationships."""
from pathlib import Path
import json
import shutil
import subprocess
import unittest


class SessionConnectionContracts(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for frontend contracts")
    def test_subagent_edges_forks_selection_and_invalid_relations(self):
        result = subprocess.run([shutil.which("node"), "tests/session_connections.cjs"],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                text=True, encoding="utf8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["passed"], 8)
