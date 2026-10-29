"""Protect complete shared development from stale or partial EXE builds."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from packet_inspector.build_info import build_info
from scripts.build_inspector import PUBLIC_FILES, package_release, source_files
from scripts.build_source import Snapshot, SourceError, Workspace, dirty_paths, git
from scripts.check_build_source import check_push, github_json
from scripts.verify.verify_build_artifact import verify


class BuildSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "shared source"
        self.root.mkdir()
        git(self.root, "init", "--quiet", "--initial-branch=main")
        git(self.root, "config", "user.name", "Build fixture")
        git(self.root, "config", "user.email", "build-fixture@example.invalid")
        git(self.root, "config", "core.autocrlf", "false")
        self.write("packet_inspector/__init__.py", 'VERSION = "0.6.14"\n')
        self.write("packet_inspector/ui/app.js", "const latest = true;\n")
        self.write(".gitignore", "build/\n.local/\n")
        self.commit()
        git(self.root, "update-ref", "refs/remotes/origin/main", "HEAD")

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf8")
        return path

    def commit(self, root=None):
        root = root or self.root
        git(root, "add", "--all")
        git(root, "commit", "--quiet", "-m", "fixture source")

    def worktree(self):
        other = Path(self.temp.name) / "other agent"
        git(self.root, "worktree", "add", "--quiet", "-b", "feat/test", str(other))
        self.addCleanup(lambda: git(self.root, "worktree", "remove", "--force", str(other), check=False))
        return other

    def snapshot(self):
        workspace = Workspace(self.root)
        return Snapshot(workspace, self.root / "build/source", source_files, workspace.validate("dev"))

    def test_dev_includes_modified_and_untracked_sources(self):
        self.write("packet_inspector/ui/app.js", "const latest = 2;\n")
        self.write("packet_inspector/window_chrome.py", "LATEST = True\n")
        snapshot = self.snapshot()
        self.assertTrue(snapshot.manifest["dirty"])
        self.assertEqual((snapshot.source / "packet_inspector/ui/app.js").read_text(), "const latest = 2;\n")
        self.assertIn("packet_inspector/window_chrome.py", snapshot.files)

    def test_release_requires_committed_complete_sources(self):
        self.write("packet_inspector/new_feature.py", "new = True\n")
        with self.assertRaisesRegex(SourceError, "must be committed"):
            Workspace(self.root).validate("release")

    def test_clean_main_is_allowed(self):
        self.assertFalse(Workspace(self.root).validate("release")["dirty"])

    def test_task_worktree_can_build_validation_package_but_cannot_release(self):
        other = self.worktree()
        self.assertEqual(Workspace(other).validate("dev")["branch"], "feat/test")
        with self.assertRaisesRegex(SourceError, "Release only from the shared main directory"):
            Workspace(other).validate("release")

    def test_unfinished_task_changes_do_not_block_integrated_main(self):
        other = self.worktree()
        (other / "packet_inspector/ui/app.js").write_text("pending")
        Workspace(self.root).validate("release")
        self.assertEqual((self.root / "packet_inspector/ui/app.js").read_text(), "const latest = true;\n")

    def test_unfinished_new_files_are_not_implicitly_included_in_main(self):
        other = self.worktree()
        (other / "packet_inspector/new_feature.py").write_text("new = True")
        Workspace(self.root).validate("release")
        self.assertNotIn("packet_inspector/new_feature.py", self.snapshot().files)

    def test_committed_task_enters_main_only_after_merging(self):
        other = self.worktree()
        (other / "packet_inspector/ui/app.js").write_text("new_feature")
        self.commit(other)
        Workspace(self.root).validate("release")
        self.assertEqual((self.root / "packet_inspector/ui/app.js").read_text(), "const latest = true;\n")
        git(self.root, "merge", "--ff-only", "--quiet", "feat/test")
        Workspace(self.root).validate("release")
        self.assertEqual((self.root / "packet_inspector/ui/app.js").read_text(), "new_feature")

    def test_task_package_cannot_omit_new_local_main_commits(self):
        other = self.worktree()
        self.write("packet_inspector/merged_feature.py", "latest integrated change")
        self.commit()
        with self.assertRaisesRegex(SourceError, "behind shared main"):
            Workspace(other).validate("dev")
        git(other, "merge", "--ff-only", "--quiet", "main")
        Workspace(other).validate("dev")

    def test_dirty_main_blocks_other_task_package(self):
        other = self.worktree()
        self.write("packet_inspector/forgotten_change.py", "pending shared change")
        with self.assertRaisesRegex(SourceError, "main has uncommitted"):
            Workspace(other).validate("dev")

    def test_task_package_waits_until_shared_directory_returns_to_main(self):
        other = self.worktree()
        git(self.root, "switch", "--quiet", "-c", "chore/shared-task")
        with self.assertRaisesRegex(SourceError, "Return the shared integration directory to main"):
            Workspace(other).validate("dev")

    def test_shared_main_change_during_task_build_invalidates_snapshot(self):
        other = self.worktree()
        workspace = Workspace(other)
        snapshot = Snapshot(workspace, other / "build/source", source_files, workspace.validate("dev"))
        self.write("packet_inspector/merged_feature.py", "new main change")
        self.commit()
        with self.assertRaisesRegex(SourceError, "Shared main changed"):
            snapshot.assert_unchanged()

    def test_ci_tag_cannot_release_unmerged_task_commit(self):
        git(self.root, "switch", "--quiet", "--detach", "HEAD")
        with patch.dict("os.environ", {"GITHUB_REF_TYPE": "tag"}):
            Workspace(self.root).validate("release")
            self.write("packet_inspector/unmerged.py", "not on main")
            self.commit()
            with self.assertRaisesRegex(SourceError, "tag must point at the latest origin/main"):
                Workspace(self.root).validate("release")

    def test_ci_configuration_is_included_in_source_identity(self):
        self.write(".github/workflows/new-check.yml", "name: Fixture check\n")
        self.assertIn(".github/workflows/new-check.yml", self.snapshot().files)

    def test_commonjs_frontend_tests_are_frozen_with_complete_source(self):
        self.write("tests/frontend.cjs", "require('node:assert').ok(true);\n")
        snapshot = self.snapshot()
        self.assertIn("tests/frontend.cjs", snapshot.files)
        self.assertEqual((snapshot.source / "tests/frontend.cjs").read_text(),
                         "require('node:assert').ok(true);\n")

    def test_source_behind_origin_main_blocks(self):
        previous = git(self.root, "rev-parse", "HEAD")
        self.write("packet_inspector/ui/app.js", "new remote feature")
        self.commit()
        git(self.root, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(self.root, "reset", "--quiet", "--hard", previous)
        with self.assertRaisesRegex(SourceError, "behind origin/main"):
            Workspace(self.root).validate("dev")

    def test_lower_version_blocks_even_if_head_is_current(self):
        self.write("packet_inspector/__init__.py", 'VERSION = "0.6.13"\n')
        with self.assertRaisesRegex(SourceError, "version is older"):
            Workspace(self.root).validate("dev")

    def test_snapshot_rejects_edit_add_and_delete_during_build(self):
        for change in ("edit", "add", "delete"):
            with self.subTest(change=change):
                snapshot = self.snapshot()
                path = self.root / "packet_inspector/ui/app.js"
                original = path.read_bytes()
                added = self.root / "packet_inspector/new_feature.py"
                if change == "edit":
                    path.write_text("changed")
                elif change == "add":
                    added.write_text("new")
                else:
                    path.unlink()
                with self.assertRaisesRegex(SourceError, "Source files changed"):
                    snapshot.assert_unchanged()
                path.write_bytes(original)
                if added.exists():
                    added.unlink()
                # Each attempt uses a new staging directory.
                import shutil
                shutil.rmtree(snapshot.source)

    def test_commit_during_build_invalidates_artifact(self):
        snapshot = self.snapshot()
        self.write("packet_inspector/new_feature.py", "new")
        self.commit()
        with self.assertRaisesRegex(SourceError, "commit changed"):
            snapshot.assert_unchanged()

    def test_shared_lock_excludes_other_builds(self):
        workspace = Workspace(self.root)
        with workspace.lock():
            with self.assertRaisesRegex(SourceError, "already building"):
                with workspace.lock():
                    self.fail("second builder entered")
        with workspace.lock():
            pass

    def test_worktree_lock_uses_same_common_repository(self):
        self.assertEqual(Workspace(self.root).common, Workspace(self.worktree()).common)

    def test_porcelain_preserves_leading_status_space(self):
        self.write("packet_inspector/ui/app.js", "changed")
        self.assertEqual(dirty_paths(self.root), ["packet_inspector/ui/app.js"])

    def test_runtime_data_is_not_frozen_or_distributed(self):
        self.write("packet_inspector/data/requests.json", "private")
        self.write(".local/secret.json", "private")
        files = [p.relative_to(self.root).as_posix() for p in source_files(self.root)]
        self.assertNotIn("packet_inspector/data/requests.json", files)
        self.assertNotIn(".local/secret.json", files)

    def test_local_development_notes_do_not_enter_or_invalidate_builds(self):
        self.write(".gitignore", "build/\n.local/\nAGENTS.md\n/docs/development.md\n")
        self.commit()
        public_sources = source_files(self.root)
        for name in ("AGENTS.md", "docs/AGENTS.md", "packet_inspector/AGENTS.md", "docs/development.md"):
            self.write(name, "Local development instructions")
        self.assertEqual(source_files(self.root), public_sources)
        self.assertEqual(dirty_paths(self.root), [])
        Workspace(self.root).validate("release")
        snapshot = self.snapshot()
        self.write("docs/development.md", "Updated internal workflow")
        self.write("AGENTS.md", "Updated local instructions")
        snapshot.assert_unchanged()

    def test_standalone_diagnostic_reports_do_not_enter_or_invalidate_builds(self):
        self.write(".gitignore", "build/\n.local/\n/scripts/windows/CodexNetworkTest-*/\n"
                   "/scripts/windows/CodexConfigReport-*/\n")
        for name in ("Test-CodexNetwork.cmd", "Collect-CodexRouting.cmd", "CodexNetworkTest-helper.cmd"):
            self.write("scripts/windows/" + name, "echo diagnostic source\n")
        self.commit()
        snapshot = self.snapshot()
        for prefix in ("CodexNetworkTest-", "CodexConfigReport-"):
            directory = "scripts/windows/" + prefix + "20261029-fixture/"
            self.write(directory + "report.json", '{"private":true}')
            self.write(directory + "nested/response.txt", "private gateway response")
            self.write(directory + "credentials.private", "private fixture")
        self.assertEqual(source_files(self.root), [self.root / name for name in sorted(snapshot.files)])
        self.assertIn("scripts/windows/Test-CodexNetwork.cmd", snapshot.files)
        self.assertIn("scripts/windows/Collect-CodexRouting.cmd", snapshot.files)
        self.assertIn("scripts/windows/CodexNetworkTest-helper.cmd", snapshot.files)
        self.assertEqual(dirty_paths(self.root), [])
        Workspace(self.root).validate("release")
        snapshot.assert_unchanged()

    def test_new_asset_types_cannot_be_silently_omitted(self):
        self.write("packet_inspector/ui/font.woff2", "fixture font")
        self.assertIn("packet_inspector/ui/font.woff2", self.snapshot().files)
        self.write("packet_inspector/ui/unhandled.resource", "new resource")
        with self.assertRaisesRegex(SourceError, "Unsupported source input"):
            source_files(self.root)

    def test_push_main_and_release_tag_must_use_shared_head(self):
        head = git(self.root, "rev-parse", "HEAD")
        zero = "0" * 40
        check_push(self.root, f"refs/heads/main {head} refs/heads/main {zero}\n")
        check_push(self.root, f"refs/tags/v0.6.14 {head} refs/tags/v0.6.14 {zero}\n")
        with self.assertRaisesRegex(SourceError, "tag must match"):
            check_push(self.root, f"refs/tags/v0.6.15 {head} refs/tags/v0.6.15 {zero}\n")
        self.write("packet_inspector/new_feature.py", "new")
        with self.assertRaisesRegex(SourceError, "must be committed"):
            check_push(self.root, f"refs/heads/main {head} refs/heads/main {zero}\n")
        # Feature branches remain available for collaboration before integration.
        check_push(self.root, f"refs/heads/agent {head} refs/heads/agent {zero}\n")

    def test_old_commit_cannot_be_published_as_new_version(self):
        previous = git(self.root, "rev-parse", "HEAD")
        self.write("packet_inspector/new_feature.py", "new")
        self.commit()
        with self.assertRaisesRegex(SourceError, "current shared main commit"):
            check_push(self.root, f"refs/heads/main {previous} refs/heads/main {'0' * 40}\n")

    def test_main_push_cannot_discard_new_remote_history(self):
        head = git(self.root, "rev-parse", "HEAD")
        remote = git(self.root, "commit-tree", "HEAD^{tree}", "-m", "unrelated remote history")
        with self.assertRaisesRegex(SourceError, "non-fast-forward main update"):
            check_push(self.root, f"refs/heads/main {head} refs/heads/main {remote}\n")

    def test_existing_release_tag_cannot_be_replaced(self):
        head = git(self.root, "rev-parse", "HEAD")
        remote = git(self.root, "commit-tree", "HEAD^{tree}", "-m", "existing published tag")
        with self.assertRaisesRegex(SourceError, "Do not replace a published version tag"):
            check_push(self.root, f"refs/tags/v0.6.14 {head} refs/tags/v0.6.14 {remote}\n")

    def unpublished_retry_fixture(self):
        old = git(self.root, "rev-parse", "HEAD")
        self.write("packet_inspector/ui/app.js", "const fixed = true;\n")
        self.commit()
        head = git(self.root, "rev-parse", "HEAD")
        git(self.root, "remote", "add", "origin", "https://github.com/example/repo.git")
        git(self.root, "config", "codex.retryUnpublishedRelease", "v0.6.14:" + old)
        return old, f"refs/tags/v0.6.14 {head} refs/tags/v0.6.14 {old}\n"

    def test_failed_unpublished_tag_retry_preserves_old_tag_and_checks_github(self):
        old, updates = self.unpublished_retry_fixture()
        runs = {"workflow_runs": [{"head_branch": "v0.6.14", "status": "completed", "conclusion": "failure"}]}
        with patch("scripts.check_build_source.github_json", side_effect=[(404, {"message": "Not Found"}), (200, runs)]) as api:
            check_push(self.root, updates)
        self.assertEqual(api.call_args_list[0].args, ("repos/example/repo/releases/tags/v0.6.14",))
        self.assertEqual(git(self.root, "rev-parse", "refs/archive/unpublished-release-tags/v0.6.14/" + old), old)

    def test_unpublished_retry_rejects_existing_release_unknown_state_and_active_or_successful_runs(self):
        _old, updates = self.unpublished_retry_fixture()
        absent = (404, {"message": "Not Found"})
        responses = [[(200, {"id": 1})], [(403, {"message": "Forbidden"})],
                     [absent, (200, {"workflow_runs": []})],
                     [absent, (200, {"workflow_runs": [{"head_branch": "v0.6.14", "status": "in_progress", "conclusion": None}]})],
                     [absent, (200, {"workflow_runs": [{"head_branch": "v0.6.14", "status": "completed", "conclusion": "success"}]})]]
        for response in responses:
            with self.subTest(response=response), patch("scripts.check_build_source.github_json", side_effect=response):
                with self.assertRaises(SourceError):
                    check_push(self.root, updates)

    def test_unpublished_retry_requires_opt_in_for_the_exact_old_tag(self):
        _old, updates = self.unpublished_retry_fixture()
        git(self.root, "config", "codex.retryUnpublishedRelease", "v0.6.14:" + "0" * 40)
        with patch("scripts.check_build_source.github_json") as api:
            with self.assertRaisesRegex(SourceError, "Do not replace a published version tag"):
                check_push(self.root, updates)
        api.assert_not_called()

    def test_github_state_lookup_requires_an_http_status_and_preserves_not_found(self):
        missing = subprocess.CompletedProcess([], 1, 'HTTP/1.1 404 Not Found\nX-Test: value\n\n{"message":"Not Found"}')
        with patch("scripts.check_build_source.shutil.which", return_value="gh"), patch("scripts.check_build_source.subprocess.run", return_value=missing):
            self.assertEqual(github_json("repos/example/repo/releases/tags/v0.6.14"), (404, {"message": "Not Found"}))
        broken = subprocess.CompletedProcess([], 1, "proxy unavailable")
        with patch("scripts.check_build_source.shutil.which", return_value="gh"), patch("scripts.check_build_source.subprocess.run", return_value=broken):
            with self.assertRaisesRegex(SourceError, "Could not verify"):
                github_json("repos/example/repo/releases/tags/v0.6.14")

    def test_artifact_verifier_rejects_stale_source_and_changed_executable(self):
        snapshot = self.snapshot()
        release = self.root / "build/artifact"
        release.mkdir()
        snapshot.write_manifest(release / "BUILD-SOURCE.json", "release")
        (release / "CodexPacketInspector.exe").write_bytes(b"synthetic current executable")
        public = release / "publish"
        public.mkdir()
        for name in PUBLIC_FILES:
            content = b"synthetic current executable" if name.endswith(".exe") else b"public documentation"
            (public / name).write_bytes(content)
        hashes = []
        for name in ("BUILD-SOURCE.json", "CodexPacketInspector.exe", *("publish/" + name for name in PUBLIC_FILES)):
            hashes.append(hashlib.sha256((release / name).read_bytes()).hexdigest() + "  " + name + "\n")
        (release / "SHA256SUMS.txt").write_text("".join(hashes), encoding="ascii")
        verify(release, self.root, "v0.6.14")
        extra = public / "CodexRequestOptimizer-Upgrade.exe"
        extra.write_bytes(b"obsolete alias")
        with self.assertRaisesRegex(SourceError, "Public release must contain only"):
            verify(release, self.root, "v0.6.14")
        extra.unlink()
        (public / "README.md").write_text("changed after validation")
        with self.assertRaisesRegex(SourceError, "hash mismatch: publish/README.md"):
            verify(release, self.root, "v0.6.14")
        (public / "README.md").write_bytes(b"public documentation")
        self.write("packet_inspector/ui/app.js", "omitted new UI")
        with self.assertRaisesRegex(SourceError, "omits current source"):
            verify(release, self.root, "v0.6.14")
        (self.root / "packet_inspector/ui/app.js").write_text("const latest = true;\n")
        (release / "CodexPacketInspector.exe").write_bytes(b"wrong exe")
        with self.assertRaisesRegex(SourceError, "hash mismatch"):
            verify(release, self.root, "v0.6.14")

    def test_public_package_excludes_development_tools_and_includes_complete_license_texts(self):
        self.write("README.md", "How to run the application")
        self.write("LICENSE", "Project license")
        self.write("docs/THIRD_PARTY_NOTICES.txt", "Bundled component notices")
        stage = self.root / "build/release"
        stage.mkdir(parents=True)
        (stage / "CodexPacketInspector.exe").write_bytes(b"formal executable")
        (stage / "CodexPacketInspector-Dev.exe").write_bytes(b"developer executable")
        licenses = self.root / "build/licenses"
        licenses.mkdir()
        (licenses / "component-LICENSE.txt").write_text("Complete component license", encoding="utf8")
        package_release(self.root, stage, licenses)
        public = stage / "publish"
        self.assertEqual({path.name for path in public.iterdir()}, set(PUBLIC_FILES))
        self.assertEqual((public / "CodexPacketInspector.exe").read_bytes(), b"formal executable")
        self.assertIn("Complete component license", (public / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf8"))

    def test_build_identity_exposes_no_paths_or_file_contents(self):
        snapshot = self.snapshot()
        manifest = self.root / "build/manifest.json"
        snapshot.write_manifest(manifest, "dev")
        identity = build_info(manifest)
        self.assertEqual(identity["source_sha256"], snapshot.manifest["source_sha256"])
        self.assertEqual(identity["version"], "0.6.14")
        self.assertNotIn("files", identity)
        self.assertEqual(build_info(self.root / "missing.json"), {})
        manifest.write_text('{"mode":"dev","git_commit":"invalid"}')
        self.assertEqual(build_info(manifest), {})


if __name__ == "__main__":
    unittest.main()
