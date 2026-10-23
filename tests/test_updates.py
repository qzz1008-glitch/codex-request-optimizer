import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch
from urllib.request import Request

from packet_inspector.router import atomic_json
from packet_inspector.updates import ASSET, LEGACY_ASSET, GitHubRedirect, Updates, version_tuple


class Reply(io.BytesIO):
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        self.close()


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.package = b"verified-upgrade-fixture"
        self.sha = hashlib.sha256(self.package).hexdigest()
        self.release = {"tag_name": "v9.1.0", "prerelease": False, "draft": False,
                        "assets": [{"id": 123, "name": ASSET, "size": len(self.package), "digest": "sha256:" + self.sha}]}
        self.opener = Mock()
        self.updates = Updates(self.directory, self.directory / "config.toml", 47844, "0.6.0", opener=self.opener)

    def release_reply(self):
        return Reply(json.dumps(self.release).encode())

    def test_check_uses_numeric_versions_and_requires_verified_upgrade_asset(self):
        self.opener.open.return_value = self.release_reply()
        result = self.updates.check()
        self.assertEqual(result["state"], "available")
        self.assertEqual(result["latest_version"], "9.1.0")
        self.assertGreater(version_tuple("v0.10.0"), version_tuple("0.9.0"))
        self.assertNotIn("Authorization", dict(self.opener.open.call_args.args[0].header_items()))
        self.release["assets"][0]["digest"] = None
        self.opener.open.return_value = self.release_reply()
        with self.assertRaises(ValueError):
            self.updates.check()

    def test_download_verifies_checksum_then_launches_same_upgrade_engine_with_local_profile(self):
        self.opener.open.side_effect = [self.release_reply(), Reply(self.package)]
        self.updates.check()
        with patch("packet_inspector.updates.github_credential", return_value="fixture-credential"), \
                patch("packet_inspector.updates.subprocess.Popen") as launch:
            state = self.updates.install()
        self.assertEqual(state["state"], "upgrading")
        arguments = launch.call_args.args[0]
        self.assertEqual(Path(arguments[0]).name, ASSET)
        self.assertIn("--quiet", arguments)
        self.assertIn("--upgrade", arguments)
        self.assertEqual(arguments[arguments.index("--port") + 1], "47844")
        self.assertEqual(arguments[arguments.index("--data-dir") + 1], str(self.directory.resolve()))
        self.assertNotIn("fixture-credential", self.updates.state_path.read_text())

    def test_selects_formal_exe_among_documents_and_legacy_assets(self):
        formal = self.release["assets"][0]
        self.release["assets"] = [
            {**formal, "name":"README.md", "id":121},
            {**formal, "name":LEGACY_ASSET, "id":122},
            formal,
            {**formal, "name":"LICENSE", "id":124},
        ]
        self.opener.open.return_value = self.release_reply()
        selected = self.updates.check()["asset"]
        self.assertEqual((selected["name"], selected["id"]), (ASSET, 123))

    def test_can_still_read_previously_published_legacy_asset(self):
        self.release["assets"][0]["name"] = LEGACY_ASSET
        self.opener.open.return_value = self.release_reply()
        self.assertEqual(self.updates.check()["asset"]["name"], LEGACY_ASSET)

    def test_tampered_download_does_not_execute_or_leave_partial_package(self):
        self.opener.open.side_effect = [self.release_reply(), Reply(b"bad")]
        self.updates.check()
        with patch("packet_inspector.updates.github_credential", return_value=""), \
                patch("packet_inspector.updates.subprocess.Popen") as launch:
            with self.assertRaises(ValueError):
                self.updates.install()
        launch.assert_not_called()
        self.assertEqual(list(self.directory.rglob("*.download")), [])

    def test_repository_change_cannot_install_cached_package_from_previous_repository(self):
        self.opener.open.return_value = self.release_reply()
        self.updates.check()
        self.updates.configure({"repository": "owner/other", "automatic": False})
        with self.assertRaises(ValueError):
            self.updates.install()
        for data in [{"repository": "https://malicious.invalid/repo"}, {"repository": None}, {"automatic": "true"}]:
            with self.assertRaises(ValueError):
                self.updates.configure(data)

    def test_local_upgrade_cannot_be_downgraded_by_old_available_state(self):
        self.opener.open.return_value = self.release_reply()
        self.updates.check()
        self.updates.current_version = "9.2.0"
        with patch("packet_inspector.updates.subprocess.Popen") as launch:
            with self.assertRaises(ValueError):
                self.updates.install()
        launch.assert_not_called()
        self.assertEqual(self.updates.status()["state"], "current")

    def test_redirect_rejects_non_github_host_and_removes_credential_for_download_host(self):
        handler = GitHubRedirect()
        request = Request("https://api.github.com/repos/owner/repo/releases/assets/1", headers={"Authorization": "Bearer fixture"})
        target = handler.redirect_request(request, None, 302, "redirect", {}, "https://release-assets.githubusercontent.com/asset.exe")
        self.assertIsNone(target.get_header("Authorization"))
        for address in ["http://github.com/file", "https://malicious.invalid/file", "https://github.com.malicious.invalid/file"]:
            with self.assertRaises(ValueError):
                handler.redirect_request(request, None, 302, "redirect", {}, address)

    def test_automatic_check_is_opt_in_and_reads_persisted_preferences(self):
        with patch.object(self.updates, "start") as start:
            self.updates.poll()
            start.assert_not_called()
            self.updates.configure({"automatic": True})
            self.updates.poll()
            start.assert_called_once_with("check")

    def test_queued_install_reports_busy_before_first_state_write(self):
        self.opener.open.return_value = self.release_reply()
        self.updates.check()
        entered, release = threading.Event(), threading.Event()

        def install():
            entered.set()
            release.wait(3)
            atomic_json(self.updates.state_path, {**self.updates.cached(), "state": "upgrading"})

        with patch.object(self.updates, "install", side_effect=install) as work:
            try:
                result = self.updates.start("install")
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.updates.cached()["state"], "available")
                self.assertEqual(result["state"], "downloading")
                self.assertEqual(self.updates.status()["state"], "downloading")
                task = self.updates.task
                self.assertEqual(self.updates.start("install")["state"], "downloading")
                self.assertIs(self.updates.task, task)
            finally:
                release.set()
                self.updates.task.join(timeout=3)
            self.assertEqual(work.call_count, 1)
        self.assertEqual(self.updates.status()["state"], "upgrading")

    def test_pending_check_reports_busy_and_returns_to_terminal_state(self):
        self.opener.open.return_value = self.release_reply()
        entered, release = threading.Event(), threading.Event()

        @contextmanager
        def lock(*_args, **_kwargs):
            entered.set()
            release.wait(3)
            yield

        with patch("packet_inspector.updates.file_lock", side_effect=lock):
            try:
                result = self.updates.start("check")
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.updates.cached()["state"], "idle")
                self.assertEqual(result["state"], "checking")
                self.assertEqual(self.updates.status()["state"], "checking")
                self.assertEqual(self.updates.start("install")["state"], "checking")
            finally:
                release.set()
                self.updates.task.join(timeout=3)
        self.assertEqual(self.updates.status()["state"], "available")

    def test_success_and_failure_from_detached_executable_are_visible_in_settings(self):
        atomic_json(self.updates.state_path, {"state": "upgrading", "upgrade_started": 10})
        atomic_json(self.directory / "upgrade-result.json", {"state": "complete", "finished": 11})
        self.assertEqual(self.updates.status()["state"], "complete")
        atomic_json(self.updates.state_path, {"state": "upgrading", "upgrade_started": 12})
        atomic_json(self.directory / "upgrade-result.json", {"state": "failed", "error": "fixture", "finished": 13})
        self.assertEqual(self.updates.status()["error"], "fixture")


if __name__ == "__main__":
    unittest.main()
