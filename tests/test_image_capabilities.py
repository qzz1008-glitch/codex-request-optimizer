"""Native image feature discovery, cache invalidation and failure boundaries."""
import os
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
import tempfile
import unittest
from unittest.mock import patch

from packet_inspector.image_capabilities import _verify_feature, verify_view_image_feature


class NativeImageCapabilitiesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cli = self.root / "codex.exe"
        self.cli.write_text("synthetic", encoding="ascii")
        _verify_feature.cache_clear()
        self.addCleanup(_verify_feature.cache_clear)
        cli = patch("packet_inspector.image_capabilities.find_codex_cli", return_value=str(self.cli))
        cli.start()
        self.addCleanup(cli.stop)

    def test_verified_effective_switch_uses_isolated_home_and_no_account_key(self):
        observed = {}
        def run(arguments, **options):
            observed.update(options)
            self.assertEqual(arguments[-4:], ["-c", "features.view_image=false", "features", "list"])
            self.assertEqual(options["env"]["CODEX_HOME"], options["cwd"])
            self.assertNotIn("OPENAI_API_KEY", options["env"])
            self.assertNotIn("CODEX_CONFIG_FILE", options["env"])
            self.assertNotEqual(Path(options["cwd"]), self.root)
            self.assertTrue(Path(options["cwd"]).is_dir())
            return CompletedProcess(arguments, 0, "view_image  stable  false\n", "")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "dummy-secret", "CODEX_CONFIG_FILE": "dummy-config"}), patch(
                "packet_inspector.image_capabilities.subprocess.run", side_effect=run):
            self.assertEqual(verify_view_image_feature(), "features.view_image")
        self.assertFalse(Path(observed["cwd"]).exists())
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["codex.exe"])

    def test_success_cached_until_native_file_changes(self):
        result = CompletedProcess([], 0, "view_image  stable  false\n", "")
        with patch("packet_inspector.image_capabilities.subprocess.run", return_value=result) as run:
            verify_view_image_feature()
            verify_view_image_feature()
            self.assertEqual(run.call_count, 1)
            info = self.cli.stat()
            os.utime(self.cli, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
            verify_view_image_feature()
            self.assertEqual(run.call_count, 2)
            self.cli.write_text("replacement-native-runtime", encoding="ascii")
            verify_view_image_feature()
            self.assertEqual(run.call_count, 3)

    def test_unknown_removed_or_ineffective_feature_is_not_accepted(self):
        for output in ("other stable false\n", "view_image removed false\n", "view_image stable true\n"):
            with self.subTest(output=output), patch("packet_inspector.image_capabilities.subprocess.run",
                    return_value=CompletedProcess([], 0, output, "")), self.assertRaisesRegex(ValueError, "未修改配置"):
                verify_view_image_feature()

    def test_failed_probe_is_not_cached_or_reported_as_success(self):
        with patch("packet_inspector.image_capabilities.subprocess.run", side_effect=[
                CompletedProcess([], 1, "", "private diagnostic"),
                CompletedProcess([], 0, "view_image stable false\n", "")]) as run:
            with self.assertRaisesRegex(ValueError, "原配置已保留"):
                verify_view_image_feature()
            self.assertEqual(verify_view_image_feature(), "features.view_image")
            self.assertEqual(run.call_count, 2)

    def test_timeout_does_not_return_a_guessed_setting(self):
        with patch("packet_inspector.image_capabilities.subprocess.run", side_effect=TimeoutExpired("codex", 10)), self.assertRaisesRegex(ValueError, "原配置已保留"):
            verify_view_image_feature()

    def test_absent_runtime_does_not_return_a_guessed_setting(self):
        with patch("packet_inspector.image_capabilities.find_codex_cli", return_value=None), self.assertRaisesRegex(ValueError, "未找到 Codex 内核"):
            verify_view_image_feature()


if __name__ == "__main__":
    unittest.main()
