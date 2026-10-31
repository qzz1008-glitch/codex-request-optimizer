"""Keep CI shards complete and make every failed subprocess block release."""
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import ci_tests


def test_ids(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from test_ids(test)
        else:
            yield test.id()


class CITestRunnerTests(unittest.TestCase):
    def test_shards_preserve_the_complete_discovery_including_duplicates(self):
        discovered = unittest.TestLoader().discover(str(ci_tests.ROOT / "tests"),
                                                   top_level_dir=str(ci_tests.ROOT))
        groups = ci_tests.plan(4)
        names = [name for group in groups for name in group["modules"]]
        self.assertEqual(len(names), len(set(names)))
        assigned = unittest.TestSuite(ci_tests.load_module(name) for name in names)
        self.assertEqual(Counter(test_ids(assigned)), Counter(test_ids(discovered)))
        self.assertEqual(sum(group["tests"] for group in groups), discovered.countTestCases())

    def test_largest_modules_are_balanced_deterministically(self):
        counts = {"a": 7, "b": 4, "c": 3, "d": 1}
        with patch.object(ci_tests, "modules", return_value=list(counts)), patch.object(
                ci_tests, "load_module", side_effect=lambda name, loader: Mock(
                    countTestCases=lambda: counts[name])):
            self.assertEqual(ci_tests.plan(2), [
                {"modules": ["a", "d"], "tests": 8},
                {"modules": ["b", "c"], "tests": 7}])

    def test_invalid_or_empty_plans_fail(self):
        with self.assertRaises(ValueError):
            ci_tests.plan(0)
        with patch.object(ci_tests, "modules", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "No test modules"):
                ci_tests.plan(4)
        with patch.object(ci_tests, "modules", return_value=["tests.missing_ci_module"]):
            with self.assertRaisesRegex(RuntimeError, "discovery failed"):
                ci_tests.plan(4)

    def test_module_level_skip_is_preserved(self):
        loader = Mock(loadTestsFromName=Mock(side_effect=unittest.SkipTest("fixture skip")))
        suite = ci_tests.load_module("fixture", loader)
        result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(len(result.skipped), 1)

    def test_failed_modules_are_reported_and_remaining_modules_still_run(self):
        outcomes = [subprocess.CompletedProcess([], 1, "", "fixture failure"),
                    subprocess.CompletedProcess([], 0, "", "")]
        with tempfile.TemporaryDirectory() as temporary, patch.object(
                ci_tests.subprocess, "run", side_effect=outcomes) as run:
            report = Path(temporary) / "timings.json"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(ci_tests.run_modules(["first", "second"], report), 1)
            self.assertIn("fixture failure", errors.getvalue())
            self.assertEqual(run.call_count, 2)
            self.assertIn("--module", run.call_args.args[0])
            self.assertEqual([row["passed"] for row in json.loads(report.read_text())], [False, True])

    def test_cli_selects_exactly_one_shard(self):
        groups = [{"modules": ["first"]}, {"modules": ["second"]}]
        with patch.object(ci_tests, "plan", return_value=groups), patch.object(
                ci_tests, "run_modules", return_value=0) as run:
            self.assertEqual(ci_tests.main(["--shards", "2", "--shard", "2"]), 0)
            run.assert_called_once_with(["second"], None)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            ci_tests.main(["--shards", "4", "--shard", "5"])
        self.assertEqual(error.exception.code, 2)

    def test_worker_error_returns_failure(self):
        suite = unittest.FunctionTestCase(lambda: self.fail("fixture error"))
        with patch.object(ci_tests, "load_module", return_value=unittest.TestSuite([suite])), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(ci_tests.main(["--module", "fixture"]), 1)
