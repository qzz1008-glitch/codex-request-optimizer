"""Independent image tool installation, migration and stdio contract checks."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import tomlkit

from packet_inspector.config_link import ConfigLink
from packet_inspector.image_config import ImageToolConfig
from packet_inspector.image_tool_install import ImageToolInstallation, find_powershell


def desktop_cli():
    directory = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
    candidates = list(directory.glob("*/codex.exe"))
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


class ToolMigrationTests(unittest.TestCase):
    def setUp(self):
        probe = patch("packet_inspector.image_config.verify_view_image_feature")
        probe.start()
        self.addCleanup(probe.stop)
        stamp = patch.object(ImageToolConfig, "native_configuration_stamp", return_value={"build": "fixture"})
        stamp.start()
        self.addCleanup(stamp.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cli = self.root / "codex.exe"
        self.cli.write_text("synthetic executable placeholder", encoding="ascii")
        self.config = self.root / "config.toml"
        self.config.write_text('model="test"\n[features]\nview_image=false\njs_repl=false\n', encoding="utf8")
        self.manager = ImageToolConfig(ConfigLink(self.config, self.root / "link.json"), self.root,
            lambda: "http://127.0.0.1:43210/relay/test")

    def enable(self):
        with patch("packet_inspector.image_config.find_codex_cli", return_value=str(self.cli)), patch.object(
                self.manager, "_validate"), self.manager.change("optimized"):
            pass

    def old_http(self):
        self.enable()
        state = deepcopy(self.manager.state)
        state.update(schema_version=2, mcp_token="old-tool-token",
                     managed_mcp={"url": "http://127.0.0.1:43210/mcp/image/old-tool-token", "tool_timeout_sec": 300})
        document = tomlkit.parse(self.config.read_text(encoding="utf8"))
        document["mcp_servers"]["isolated_vision"] = state["managed_mcp"]
        self.config.write_text(tomlkit.dumps(document), encoding="utf8")
        self.manager.installation.uninstall()
        self.manager._save(state)

    def test_http_registration_migrates_and_uninstall_enables_native(self):
        self.old_http()
        profile = self.manager.profile_path.read_bytes()
        with patch.object(self.manager, "_validate"), self.manager.change("optimized") as changed:
            self.assertTrue(changed)
        registered = tomllib.loads(self.config.read_text(encoding="utf8"))["mcp_servers"]["isolated_vision"]
        self.assertNotIn("url", registered)
        self.assertIn("server.ps1", registered["args"][-1])
        self.assertTrue(Path(registered["args"][-1]).is_file())
        self.assertEqual(self.manager.profile_path.read_bytes(), profile)
        self.assertNotIn("mcp_token", self.manager.state)
        with self.manager.change("native"):
            pass
        data = tomllib.loads(self.config.read_text(encoding="utf8"))
        self.assertNotIn("view_image", data.get("features", {}))
        self.assertNotIn("view_image", data.get("tools", {}))
        self.assertIs(data["features"]["js_repl"], False)
        self.assertNotIn("isolated_vision", data.get("mcp_servers", {}))
        self.assertFalse(self.manager.installation.directory.exists())

    def test_refresh_relocates_cli_after_desktop_update_without_changing_connection(self):
        self.enable()
        missing = self.root / "removed-cli" / "codex.exe"
        available = self.root / "new-cli" / "codex.exe"
        available.parent.mkdir()
        available.write_text("synthetic executable placeholder", encoding="ascii")
        state = deepcopy(self.manager.state)
        state.update(schema_version=4, cli=str(missing))
        state["managed_mcp"]["env"]["CODEX_CLI_PATH"] = str(missing)
        self.manager._save(state)
        document = tomlkit.parse(self.config.read_text(encoding="utf8"))
        document["mcp_servers"]["isolated_vision"] = state["managed_mcp"]
        document["model_provider"] = "user-provider"
        document["model_providers"] = {"user-provider": {"base_url": "https://example.invalid/v1", "env_key": "TEST_KEY"}}
        before = tomllib.loads(tomlkit.dumps(document))
        self.config.write_text(tomlkit.dumps(document), encoding="utf8")
        profile = self.manager.profile_path.read_bytes()
        with patch("packet_inspector.image_config.find_codex_cli", return_value=str(available)), patch.object(
                self.manager, "_validate"), self.manager.change("optimized") as changed:
            self.assertTrue(changed)
        after = tomllib.loads(self.config.read_text(encoding="utf8"))
        self.assertEqual(self.manager.state["cli"], str(available))
        self.assertEqual(after["mcp_servers"]["isolated_vision"]["env"]["CODEX_CLI_PATH"], str(available))
        self.assertEqual(after["model_provider"], before["model_provider"])
        self.assertEqual(after["model_providers"], before["model_providers"])
        self.assertEqual(self.manager.profile_path.read_bytes(), profile)
        with patch("packet_inspector.image_config.find_codex_cli", side_effect=AssertionError("valid CLI must be kept")), self.manager.change("optimized") as changed:
            self.assertFalse(changed)

    def test_refresh_without_a_replacement_cli_preserves_installed_tool_and_config(self):
        self.enable()
        self.cli.unlink()
        before, state = self.config.read_bytes(), self.manager.path.read_bytes()
        with patch("packet_inspector.image_config.find_codex_cli", return_value=None), self.assertRaisesRegex(ValueError, "未找到 Codex CLI"):
            with self.manager.change("optimized"):
                pass
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.manager.path.read_bytes(), state)
        self.assertTrue(self.manager.installation.directory.exists())

    def test_http_migration_rolls_back_registration_state_and_files(self):
        self.old_http()
        before, state = self.config.read_bytes(), self.manager.path.read_bytes()
        with patch.object(self.manager, "_validate", side_effect=ValueError("CLI unavailable")), self.assertRaises(ValueError):
            with self.manager.change("optimized"):
                pass
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.manager.path.read_bytes(), state)
        self.assertFalse(self.manager.installation.directory.exists())

    def test_settings_failure_also_rolls_back_http_migration(self):
        self.old_http()
        before, state = self.config.read_bytes(), self.manager.path.read_bytes()
        with patch.object(self.manager, "_validate"), self.assertRaisesRegex(RuntimeError, "settings failed"):
            with self.manager.change("optimized"):
                raise RuntimeError("settings failed")
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.manager.path.read_bytes(), state)
        self.assertFalse(self.manager.installation.directory.exists())

    def test_native_selection_enables_previously_disabled_native_tool(self):
        with self.manager.change("native"):
            pass
        data = tomllib.loads(self.config.read_text(encoding="utf8"))
        self.assertNotIn("view_image", data.get("features", {}))
        self.assertNotIn("view_image", data.get("tools", {}))

    def test_failed_uninstall_keeps_optimized_settings_and_registration(self):
        self.enable()
        before, state = self.config.read_bytes(), self.manager.path.read_bytes()
        with patch.object(self.manager.installation, "uninstall", side_effect=PermissionError("file busy")), self.assertRaises(PermissionError):
            with self.manager.change("native"):
                pass
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.manager.path.read_bytes(), state)
        self.assertTrue(self.manager.installation.manifest.exists())

    def test_installation_does_not_overwrite_or_delete_user_files(self):
        installation = self.manager.installation
        installation.directory.mkdir(parents=True)
        script = installation.directory / "server.ps1"
        script.write_text("user-owned file", encoding="utf8")
        with self.assertRaises(ValueError):
            installation.install("fake-codex")
        self.assertEqual(script.read_text(encoding="utf8"), "user-owned file")

    def test_uninstall_preserves_unrelated_files(self):
        self.enable()
        note = self.manager.installation.directory / "user-note.txt"
        note.write_text("keep", encoding="utf8")
        with self.manager.change("native"):
            pass
        self.assertEqual(note.read_text(encoding="utf8"), "keep")
        self.assertFalse((note.parent / "server.ps1").exists())

    def test_upgrade_adds_native_session_helper_to_an_owned_legacy_installation(self):
        self.enable()
        installation = self.manager.installation
        state = json.loads(installation.manifest.read_text(encoding="utf8"))
        state["files"].pop("image_session.ps1")
        (installation.directory / "image_session.ps1").unlink()
        installation.manifest.write_text(json.dumps(state), encoding="utf8")
        with self.manager.change("optimized"):
            pass
        self.assertTrue((installation.directory / "image_session.ps1").is_file())
        self.assertIn("image_session.ps1", json.loads(installation.manifest.read_text(encoding="utf8"))["files"])

    def test_upgrade_does_not_overwrite_an_unowned_session_helper(self):
        installation = self.manager.installation
        installation.install("fake-codex")
        state = json.loads(installation.manifest.read_text(encoding="utf8"))
        state["files"].pop("image_session.ps1")
        installation.manifest.write_text(json.dumps(state), encoding="utf8")
        helper = installation.directory / "image_session.ps1"
        helper.write_text("user-owned helper", encoding="utf8")
        with self.assertRaises(ValueError):
            installation.install("fake-codex")
        self.assertEqual(helper.read_text(encoding="utf8"), "user-owned helper")

    def legacy_native_feature(self):
        self.enable()
        state = deepcopy(self.manager.state)
        state["originals"] = [entry for entry in state["originals"] if entry["table"] != "features"]
        self.manager._save(state)
        data = tomllib.loads(self.config.read_text(encoding="utf-8"))
        data["features"]["view_image"] = True
        self.config.write_text(tomlkit.dumps(data), encoding="utf-8")

    def test_legacy_install_disables_effective_feature_and_records_its_original_value(self):
        self.legacy_native_feature()
        registration = deepcopy(self.manager.state["managed_mcp"])
        with patch.object(self.manager, "_validate"), self.manager.change("optimized") as changed:
            self.assertTrue(changed)
        data = tomllib.loads(self.config.read_text(encoding="utf-8"))
        self.assertIs(data["features"]["view_image"], False)
        self.assertIs(data["features"]["js_repl"], False)
        self.assertEqual(data["mcp_servers"]["isolated_vision"], registration)
        with self.manager.change("native"):
            pass
        self.assertIs(tomllib.loads(self.config.read_text(encoding="utf-8"))["features"]["view_image"], True)

    def test_feature_migration_rolls_back_when_caller_settings_fail(self):
        self.legacy_native_feature()
        config, state = self.config.read_bytes(), self.manager.path.read_bytes()
        with patch.object(self.manager, "_validate"), self.assertRaisesRegex(RuntimeError, "settings failed"):
            with self.manager.change("optimized"):
                raise RuntimeError("settings failed")
        self.assertEqual(self.config.read_bytes(), config)
        self.assertEqual(self.manager.path.read_bytes(), state)

    def test_user_changes_to_managed_feature_are_preserved(self):
        self.enable()
        data = tomllib.loads(self.config.read_text(encoding="utf-8"))
        data["features"]["view_image"] = True
        self.config.write_text(tomlkit.dumps(data), encoding="utf-8")
        before = self.config.read_bytes()
        for mode in ("optimized", "native", "disabled"):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "开关已被修改"):
                with self.manager.change(mode):
                    pass
            self.assertEqual(self.config.read_bytes(), before)

    def test_legacy_disabled_mode_migrates_and_restores_the_effective_feature(self):
        with self.manager.change("disabled"):
            pass
        for name in ("native_feature_present", "native_feature", "features_created"):
            self.manager.state["disabled_original"].pop(name)
        self.manager._save(self.manager.state)
        data = tomllib.loads(self.config.read_text(encoding="utf-8"))
        data["features"]["view_image"] = True
        self.config.write_text(tomlkit.dumps(data), encoding="utf-8")
        with self.manager.change("disabled") as changed:
            self.assertTrue(changed)
        self.assertIs(tomllib.loads(self.config.read_text(encoding="utf-8"))["features"]["view_image"], False)
        with self.manager.change("native"):
            pass
        self.assertIs(tomllib.loads(self.config.read_text(encoding="utf-8"))["features"]["view_image"], True)


    def test_failed_capability_probe_leaves_configuration_and_tool_untouched(self):
        config = self.config.read_bytes()
        for mode in ("disabled", "optimized"):
            with self.subTest(mode=mode), patch("packet_inspector.image_config.verify_view_image_feature",
                    side_effect=ValueError("native capability unavailable")), self.assertRaisesRegex(ValueError, "capability"):
                with self.manager.change(mode):
                    pass
            self.assertEqual(self.config.read_bytes(), config)
            self.assertFalse(self.manager.path.exists())
            self.assertFalse(self.manager.installation.directory.exists())

    def test_successful_detection_is_reused_until_the_build_or_runtime_changes(self):
        self.enable()
        with patch("packet_inspector.image_config.verify_view_image_feature") as probe, patch.object(
                self.manager, "native_configuration_stamp", return_value={"build": "fixture"}) as stamp:
            with self.manager.change("optimized"):
                pass
            probe.assert_not_called()
            stamp.return_value = {"build": "next-release"}
            with self.manager.change("optimized"):
                pass
            self.assertEqual(probe.call_count, 1)
            with self.manager.change("optimized"):
                pass
            self.assertEqual(probe.call_count, 1)
            stamp.return_value = {"build": "next-release", "runtime": "new-core"}
            with self.manager.change("optimized"):
                pass
            self.assertEqual(probe.call_count, 2)

    def test_metadata_only_upgrade_check_rolls_back_when_settings_fail(self):
        self.enable()
        config, state = self.config.read_bytes(), self.manager.path.read_bytes()
        with patch.object(self.manager, "native_configuration_stamp", return_value={"build": "next-release"}), self.assertRaisesRegex(RuntimeError, "settings failed"):
            with self.manager.change("optimized") as changed:
                self.assertFalse(changed)
                self.assertEqual(self.manager.state["native_capability"], {"build": "next-release"})
                raise RuntimeError("settings failed")
        self.assertEqual(self.config.read_bytes(), config)
        self.assertEqual(self.manager.path.read_bytes(), state)

    def legacy_tools_alias(self):
        self.enable()
        document = tomlkit.parse(self.config.read_text(encoding="utf8"))
        document["tools"] = {"view_image": False, "web_search": False}
        self.config.write_text(tomlkit.dumps(document), encoding="utf8")
        state = deepcopy(self.manager.state)
        state["originals"].insert(0, {"table": "tools", "name": "view_image", "present": True, "value": False})
        self.manager._save(state)

    def test_optimized_refresh_removes_only_legacy_alias_and_is_idempotent(self):
        self.legacy_tools_alias()
        with patch.object(self.manager, "_validate"), self.manager.change("optimized") as changed:
            self.assertTrue(changed)
        data = tomllib.loads(self.config.read_text(encoding="utf8"))
        self.assertEqual(data["tools"], {"web_search": False})
        self.assertIs(data["features"]["view_image"], False)
        with patch.object(self.manager, "_validate"), self.manager.change("optimized") as changed:
            self.assertFalse(changed)
        with self.manager.change("native"):
            pass
        data = tomllib.loads(self.config.read_text(encoding="utf8"))
        self.assertEqual(data["tools"], {"web_search": False})
        self.assertNotIn("view_image", data.get("features", {}))

    def test_removed_legacy_alias_does_not_block_existing_installation(self):
        self.legacy_tools_alias()
        document = tomlkit.parse(self.config.read_text(encoding="utf8"))
        document["tools"].pop("view_image")
        self.config.write_text(tomlkit.dumps(document), encoding="utf8")
        with patch.object(self.manager, "_validate"), self.manager.change("optimized"):
            pass
        with self.manager.change("native"):
            pass
        data = tomllib.loads(self.config.read_text(encoding="utf8"))
        self.assertNotIn("view_image", data["tools"])
        self.assertNotIn("view_image", data.get("features", {}))

    def test_disabled_refresh_removes_legacy_alias_without_enabling_images(self):
        with self.manager.change("disabled"):
            pass
        document = tomlkit.parse(self.config.read_text(encoding="utf8"))
        document["tools"] = {"view_image": False, "web_search": False}
        self.config.write_text(tomlkit.dumps(document), encoding="utf8")
        with self.manager.change("disabled") as changed:
            self.assertTrue(changed)
        data = tomllib.loads(self.config.read_text(encoding="utf8"))
        self.assertEqual(data["tools"], {"web_search": False})
        self.assertIs(data["features"]["view_image"], False)
        with self.manager.change("disabled") as changed:
            self.assertFalse(changed)

    def test_legacy_alias_cleanup_rolls_back_with_caller_settings(self):
        self.legacy_tools_alias()
        config, state = self.config.read_bytes(), self.manager.path.read_bytes()
        with patch.object(self.manager, "_validate"), self.assertRaisesRegex(RuntimeError, "settings failed"):
            with self.manager.change("optimized"):
                raise RuntimeError("settings failed")
        self.assertEqual(self.config.read_bytes(), config)
        self.assertEqual(self.manager.path.read_bytes(), state)


@unittest.skipUnless(os.name == "nt" and desktop_cli(), "Installed native Codex CLI")
class NativeImageFeatureTests(unittest.TestCase):
    def test_disabled_mode_changes_codex_effective_feature_and_native_restores_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            config.write_text('[tools]\nview_image=false\n', encoding="utf-8")
            manager = ImageToolConfig(ConfigLink(config, root / "link.json"), root,
                                     lambda: "http://127.0.0.1:9/relay/test")
            def enabled():
                result = subprocess.run([str(desktop_cli()), "features", "list"],
                    env={**os.environ, "CODEX_HOME": str(root)}, cwd=root, capture_output=True,
                    text=True, encoding="utf-8", timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.assertEqual(result.returncode, 0, result.stderr[:700])
                return next(line.split()[-1] for line in result.stdout.splitlines() if line.startswith("view_image "))
            self.assertEqual(enabled(), "true")
            with manager.change("disabled"):
                pass
            self.assertEqual(enabled(), "false")
            with manager.change("native"):
                pass
            self.assertEqual(enabled(), "true")


@unittest.skipUnless(os.name == "nt", "Windows PowerShell stdio tool")
class StdioRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        installation = ImageToolInstallation(self.root)
        self.registration = installation.install("not-a-real-codex")
        self.script = Path(self.registration["args"][-1])

    def stdio(self, messages):
        value = "".join((json.dumps(item, ensure_ascii=False) if not isinstance(item, str) else item) + "\n" for item in messages)
        result = subprocess.run([self.registration["command"], *self.registration["args"]], input=value,
            env={**os.environ, **self.registration["env"]}, capture_output=True, text=True, encoding="utf8",
            timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr[:700])
        return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

    @staticmethod
    def initialize():
        return {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}

    def test_stdio_protocol_and_eof_need_no_inspector_or_python_runtime(self):
        replies = self.stdio([self.initialize(), {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": "list", "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"}])
        self.assertEqual(len(replies), 3)
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "codex-isolated-vision")
        tool = replies[1]["result"]["tools"][0]
        self.assertEqual(tool["name"], "analyze_image")
        self.assertEqual(tool["inputSchema"]["required"], ["path"])
        self.assertEqual(replies[2]["result"], {})

    def test_invalid_input_is_text_only_and_calls_are_bounded(self):
        messages = [self.initialize()]
        messages.extend({"jsonrpc": "2.0", "id": i + 2, "method": "tools/call",
            "params": {"name": "analyze_image", "arguments": {"path": "relative.png"}}} for i in range(25))
        replies = self.stdio(messages)
        self.assertEqual(len(replies), 26)
        for reply in replies[1:]:
            self.assertTrue(reply["result"]["isError"])
            self.assertEqual([c["type"] for c in reply["result"]["content"]], ["text"])
        self.assertIn("limit reached", replies[-1]["result"]["content"][0]["text"])

    def test_malformed_json_and_unknown_methods_do_not_crash(self):
        replies = self.stdio(["{malformed", self.initialize(), {"jsonrpc": "2.0", "id": 8, "method": "unknown"}])
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[-1]["error"]["code"], -32601)

    def test_parent_identity_uses_each_mcp_call_not_shared_process_environment(self):
        parent = "01a15d9a-f5e3-73c1-b4e7-87b3c5db9a3c"
        other = "01a166b7-e4a7-7550-8eaa-06e64a78e986"
        report = self.powershell("""
function Invoke-ImageAnalysis($Arguments, [string]$ParentThreadId) { return ('parent=' + $ParentThreadId) }
$env:CODEX_THREAD_ID = '00000000-0000-0000-0000-000000000099'
$null = Invoke-McpRequest ([pscustomobject]@{jsonrpc='2.0';id=1;method='initialize';params=@{}})
$results = @(foreach ($value in @('""" + parent.upper() + """','""" + other + """','invalid',$null,17,@('""" + parent + """'),
    ('""" + parent + """' + "`n"),('""" + parent + """' + '";ignored=true'))) {
    $request = [pscustomobject]@{jsonrpc='2.0';id=2;method='tools/call';params=[pscustomobject]@{
        name='analyze_image';arguments=[pscustomobject]@{path='not-read.png'};_meta=[pscustomobject]@{threadId=$value}}}
    $reply = Invoke-McpRequest $request
    $reply.result.content[0].text
})
@{parents=$results} | ConvertTo-Json -Compress
""")
        self.assertEqual(report["parents"], ["parent=" + parent, "parent=" + other, *["parent="] * 6])

    def powershell(self, body):
        wrapper = self.root / "check.ps1"
        source = str(self.script).replace("'", "''")
        wrapper.write_text("$ErrorActionPreference='Stop'\n. '" + source + "' -LibraryMode\n" + body,
                           encoding="utf-8-sig")
        result = subprocess.run([find_powershell(), "-NoLogo", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(wrapper)], capture_output=True, text=True,
            encoding="utf8", timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr[:1000])
        return json.loads(result.stdout)

    @staticmethod
    def native_stubs():
        return """
function Find-CodexCli { return 'fake-codex' }
$script:ThreadCount = 0
function Get-ImageHost($Cli) {
    if (-not $script:FakeHost) {
        $work=Join-Path ([IO.Path]::GetTempPath()) ('codex-isolated-vision-'+[guid]::NewGuid().ToString('N'))
        $null=New-Item -ItemType Directory -Path $work
        $script:FakeHost=@{Work=$work}
    }
    return $script:FakeHost
}
function Invoke-ImageRpc($HostState,$Method,$Params,$Deadline) {
    if ($Method -eq 'thread/start') {
        $script:ThreadCount++
        $script:ThreadParams=$Params
        return @{thread=@{id=('thread'+$script:ThreadCount)}}
    }
}
function Invoke-ImageTurn($HostState,$Session,$Instruction) {
    $script:ObservedUpload=$Session.Upload
    $script:ObservedInput=$Instruction
    $Session.Turns++
    return 'Visible error text'
}
"""

    def test_analysis_uses_an_ephemeral_native_thread_and_disables_other_mcp_servers(self):
        image = self.root / "synthetic.png"
        image.write_bytes(b"synthetic-image-fixture")
        path = str(image).replace("'", "''")
        report = self.powershell(self.native_stubs() + """
function Invoke-ToolProcess($Executable, $Arguments, $TimeoutSeconds, $WorkingDirectory, $InputText) {
    return @{ExitCode=0;Output='[{"name":"isolated_vision","transport":{"type":"stdio","command":"powershell.exe"}},{"name":"codex_app","transport":{"type":"streamable_http","url":"http://127.0.0.1:9/mcp"}}]'}
}
$value = Invoke-ImageAnalysis ([pscustomobject]@{path='""" + path + """';instruction='Read the error'}) '01a15d9a-f5e3-73c1-b4e7-87b3c5db9a3c'
$override=Get-ImageMcpOverride 'fake-codex' $script:FakeHost.Work
Remove-ImageWork $script:FakeHost.Work
@{answer=$value;ephemeral=$script:ThreadParams.ephemeral;image=$script:ObservedUpload;
  override=$override;prompt=$script:ObservedInput;
  metadata=$script:ThreadParams.config.responses_api_metadata;
  cleaned=(-not(Test-Path -LiteralPath $script:FakeHost.Work))} | ConvertTo-Json -Depth 8 -Compress
""")
        self.assertEqual(report["answer"], "Visible error text")
        self.assertTrue(report["ephemeral"])
        self.assertEqual(report["prompt"], "Read the error")
        self.assertNotEqual(Path(report["image"]).resolve(), image.resolve())
        self.assertTrue(report["cleaned"])
        servers = tomllib.loads(report["override"])["mcp_servers"]
        self.assertEqual(servers, {"isolated_vision": {"enabled": False, "command": "powershell.exe"},
                                   "codex_app": {"enabled": False, "url": "http://127.0.0.1:9/mcp"}})
        self.assertEqual(report["metadata"], {
            "codex_inspector_request_kind": "image_analysis", "codex_inspector_image_session": "native",
            "codex_inspector_image_source_path": str(image.resolve()),
            "codex_inspector_parent_thread_id": "01a15d9a-f5e3-73c1-b4e7-87b3c5db9a3c"})
        cli = desktop_cli()
        if cli:
            # The listed servers intentionally have no definitions in this home.
            # Codex must accept the actual override generated by the tool.
            home = self.root / "native-cli-home"
            home.mkdir()
            (home / "config.toml").write_text("", encoding="utf-8")
            result = subprocess.run([str(cli), "-c", report["override"], "mcp", "list", "--json"],
                env={**os.environ, "CODEX_HOME": str(home)}, cwd=home, capture_output=True,
                text=True, encoding="utf-8", timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(result.returncode, 0, result.stderr[:700])
            listed = json.loads(result.stdout)
            self.assertEqual({server["name"] for server in listed}, set(servers))
            self.assertTrue(all(server["enabled"] is False for server in listed))

    def test_compressed_upload_keeps_explicit_source_after_temporary_copy_is_removed(self):
        image = self.root / "source 原图.png"
        with image.open("wb") as fixture:
            fixture.truncate(409601)
        path = str(image).replace("'", "''")
        report = self.powershell(self.native_stubs() + """
function Invoke-ToolProcess($Executable, $Arguments, $TimeoutSeconds, $WorkingDirectory, $InputText) {
    if ('-OutputPath' -in $Arguments) {
        $target = $Arguments[[array]::IndexOf($Arguments,'-OutputPath')+1]
        [IO.File]::WriteAllText($target,'synthetic compressed fixture')
        return @{ExitCode=0;Output=''}
    }
}
$answer = Invoke-ImageAnalysis ([pscustomobject]@{path='""" + path + """'}) $null
$upload = $script:ObservedUpload
Remove-ImageWork $script:FakeHost.Work
@{answer=$answer;upload=$upload;uploadExists=(Test-Path -LiteralPath $upload);
  sourceExists=(Test-Path -LiteralPath '""" + path + """');
  metadata=$script:ThreadParams.config.responses_api_metadata} | ConvertTo-Json -Depth 8 -Compress
""")
        self.assertEqual(report["answer"], "Visible error text")
        self.assertTrue(Path(report["upload"]).name.endswith("-upload.jpg"))
        self.assertFalse(report["uploadExists"])
        self.assertTrue(report["sourceExists"])
        self.assertEqual(report["metadata"]["codex_inspector_image_source_path"],
                         str(image.resolve()))

    def test_reuse_is_scoped_to_parent_and_unchanged_image_and_unknown_parents_stay_separate(self):
        image = self.root / "synthetic.png"
        image.write_text("first image fixture", encoding="utf8")
        path = str(image).replace("'", "''")
        report = self.powershell(self.native_stubs() + """
$request=[pscustomobject]@{path='""" + path + """';instruction='first question'}
$null=Invoke-ImageAnalysis $request '01a15d9a-f5e3-73c1-b4e7-87b3c5db9a3c'
$request.instruction='second question'
$null=Invoke-ImageAnalysis $request '01a15d9a-f5e3-73c1-b4e7-87b3c5db9a3c'
$reused=$script:ThreadCount
$null=Invoke-ImageAnalysis $request '01a13966-2d51-78f1-9dce-fc84bdee542c'
[IO.File]::WriteAllText($request.path,'changed image fixture')
$null=Invoke-ImageAnalysis $request '01a15d9a-f5e3-73c1-b4e7-87b3c5db9a3c'
$null=Invoke-ImageAnalysis $request $null
$null=Invoke-ImageAnalysis $request $null
Remove-ImageWork $script:FakeHost.Work
@{reused=$reused;created=$script:ThreadCount;retained=$script:ImageSessions.Count} | ConvertTo-Json -Compress
""")
        self.assertEqual(report, {"reused": 1, "created": 5, "retained": 3})

    def test_rollover_requires_the_designated_error_after_a_successful_turn(self):
        image = self.root / "synthetic.png"
        image.write_text("image fixture", encoding="utf8")
        path = str(image).replace("'", "''")
        report = self.powershell(self.native_stubs() + """
function Invoke-ImageTurn($HostState,$Session,$Instruction) {
    if ($Instruction -eq 'capacity') { throw 'ordinary capacity failure' }
    if ($Instruction -eq 'large' -and $Session.Turns -gt 0 -or $Instruction -eq 'too large even when fresh') {
        $failure=New-Object InvalidOperationException 'image_session_size_limit'
        $failure.Data['ImageSessionLimit']=$true
        throw $failure
    }
    $Session.Turns++
    return 'answer'
}
$parent='01a15d9a-f5e3-73c1-b4e7-87b3c5db9a3c'
$request=[pscustomobject]@{path='""" + path + """';instruction='first question'}
$null=Invoke-ImageAnalysis $request $parent
$request.instruction='capacity'
try { $null=Invoke-ImageAnalysis $request $parent } catch {}
$afterCapacity=$script:ThreadCount
$request.instruction='large'
$null=Invoke-ImageAnalysis $request $parent
$afterRollover=$script:ThreadCount
$request.instruction='too large even when fresh'
try { $null=Invoke-ImageAnalysis $request $parent } catch { $failed=$true }
Remove-ImageWork $script:FakeHost.Work
@{capacity=$afterCapacity;rollover=$afterRollover;final=$script:ThreadCount;failed=$failed} | ConvertTo-Json -Compress
""")
        self.assertEqual(report, {"capacity": 1, "rollover": 2, "final": 3, "failed": True})

    def test_native_turn_sends_only_new_input_and_returns_the_last_message_for_its_thread(self):
        report = self.powershell("""
$hostState=@{Events=(New-Object 'System.Collections.Generic.Queue[object]')}
function Invoke-ImageRpc($HostState,$Method,$Params,$Deadline) {
    $script:Inputs=$Params.input
    foreach ($entry in @(@{thread='other';text='other thread'},@{thread='vision';text='commentary'},@{thread='vision';text='final answer'})) {
        $HostState.Events.Enqueue([pscustomobject]@{method='item/completed';params=@{
            threadId=$entry.thread;turnId='turn';item=@{id=$entry.text;type='agentMessage';text=$entry.text}}})
    }
    $HostState.Events.Enqueue([pscustomobject]@{method='turn/completed';params=@{threadId='vision';turn=@{id='turn';status='completed'}}})
    return @{turn=@{id='turn'}}
}
$session=@{ThreadId='vision';Upload='C:\\fixture.png';Turns=0}
$first=Invoke-ImageTurn $hostState $session 'first'
$firstInputs=$script:Inputs
$second=Invoke-ImageTurn $hostState $session 'follow up'
@{first=$first;second=$second;firstInputs=$firstInputs;secondInputs=$script:Inputs;turns=$session.Turns} | ConvertTo-Json -Depth 8 -Compress
""")
        self.assertEqual(report["first"], "final answer")
        self.assertEqual(report["second"], "final answer")
        self.assertEqual([p["type"] for p in report["firstInputs"]], ["text", "localImage"])
        self.assertEqual(report["secondInputs"], [{"type": "text", "text": "follow up"}])
        self.assertEqual(report["turns"], 2)

    def test_call_deadline_does_not_reset_for_a_second_native_turn(self):
        report = self.powershell("""
$pending=New-Object 'System.Threading.Tasks.TaskCompletionSource[string]'
$hostState=@{ReadTask=$pending.Task}
$script:ImageCallDeadline=[datetime]::UtcNow.AddSeconds(-1)
try { $null=Read-ImageEvent $hostState ([datetime]::UtcNow.AddSeconds(240)) }
catch { @{unusable=[bool]$_.Exception.Data['ImageHostUnusable'];message=$_.Exception.Message} | ConvertTo-Json -Compress }
""")
        self.assertTrue(report["unusable"])
        self.assertIn("timed out", report["message"])

    def test_native_process_quoting_preserves_spaces_quotes_and_unicode(self):
        echo = self.root / "echo.ps1"
        echo.write_text("[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)\n"
                        "[Console]::Out.WriteLine((ConvertTo-Json -InputObject @($args) -Compress))\n", encoding="utf-8-sig")
        expected = ['path with spaces', 'quote"inside', 'trailing\\', '中文图像']
        literals = ",".join("'" + item.replace("'", "''") + "'" for item in expected)
        report = self.powershell("$arguments=@('-NoProfile','-File','" + str(echo).replace("'", "''") + "'," + literals + ")\n"
            "$result=Invoke-ToolProcess '" + find_powershell().replace("'", "''") + "' $arguments 10 $PSScriptRoot\n"
            "$values=ConvertFrom-Json -InputObject $result.Output\n"
            "@{code=$result.ExitCode;values=@(foreach($value in $values){[string]$value})} | ConvertTo-Json -Depth 5 -Compress\n")
        self.assertEqual(report["code"], 0)
        self.assertEqual(report["values"], expected)

    def test_batch_cli_receives_multiline_native_rpc_input_without_shell_interpolation(self):
        echo = self.root / "echo.py"
        echo.write_text('''import json,sys
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
if sys.argv[1] == "mcp":
    print("[]")
    sys.exit(0)
def send(value):
    print(json.dumps(value,ensure_ascii=False),flush=True)
config={}
for line in sys.stdin:
    message=json.loads(line)
    method=message["method"]
    if "id" not in message: continue
    result={}
    if method=="thread/start":
        config=message["params"]["config"]
        result={"thread":{"id":"native-thread"}}
    if method=="turn/start": result={"turn":{"id":"native-turn"}}
    send({"id":message["id"],"result":result})
    if method=="turn/start":
        report={"input":message["params"]["input"],"config":config}
        send({"method":"item/completed","params":{"threadId":"native-thread","turnId":"native-turn",
            "item":{"type":"agentMessage","id":"answer","text":json.dumps(report,ensure_ascii=False)}}})
        send({"method":"turn/completed","params":{"threadId":"native-thread",
            "turn":{"id":"native-turn","status":"completed"}}})
''', encoding="utf-8")
        wrapper = self.root / "codex.cmd"
        wrapper.write_text('@echo off\r\n"%CODEX_TEST_PYTHON%" "%CODEX_TEST_ECHO%" %*\r\n', encoding="ascii")
        image = self.root / "synthetic image.png"
        image.write_bytes(b"synthetic-image-fixture")
        instruction = "Read synthetic text.\n\n中文 'quotes' & pipes | %PATH%"
        request = json.dumps({"path": str(image), "instruction": instruction}, ensure_ascii=False)
        report = self.powershell("$env:CODEX_TEST_PYTHON='" + sys.executable.replace("'", "''") + "'\n"
                                 "$env:CODEX_TEST_ECHO='" + str(echo).replace("'", "''") + "'\n"
                                 "$env:CODEX_CLI_PATH='" + str(wrapper).replace("'", "''") + "'\n"
                                 "$request=ConvertFrom-Json -InputObject '" + request.replace("'", "''") + "'\n"
                                 "try { Invoke-ImageAnalysis $request $null } finally { Close-ImageSessions }\n")
        self.assertEqual(report["input"][0], {"type": "text", "text": instruction})
        self.assertEqual(report["input"][1]["type"], "localImage")
        metadata = report["config"]["responses_api_metadata"]
        self.assertEqual(metadata["codex_inspector_request_kind"], "image_analysis")
        self.assertEqual(metadata["codex_inspector_image_source_path"], str(image.resolve()))

    def test_analysis_failure_returns_bounded_redacted_stderr(self):
        image = self.root / "synthetic.png"
        image.write_bytes(b"synthetic-image-fixture")
        path = str(image).replace("'", "''")
        report = self.powershell("""
function Find-CodexCli { return 'fake-codex' }
function Invoke-ToolProcess($Executable, $Arguments, $TimeoutSeconds, $WorkingDirectory, $InputText) {
    return @{ExitCode=1;Output='';Error=(('prefix ' * 250) +
        'Windows sandbox failed: CreateProcessWithLogonW failed: 1385; Bearer fake-secret sk-test-secret ' +
        'http://127.0.0.1/relay/private-token/p/test?access_token=secret&other=ok ' +
        'data:image/png;base64,AAAA')}
}
$reply=Invoke-McpRequest ([pscustomobject]@{jsonrpc='2.0';id=1;method='initialize';params=@{protocolVersion='2025-06-18'}})
$reply=Invoke-McpRequest ([pscustomobject]@{jsonrpc='2.0';id=2;method='tools/call';params=@{name='analyze_image';
    arguments=[pscustomobject]@{path='""" + path + """';instruction='Read synthetic text'}}})
@{error=$reply.result.isError;detail=$reply.result.content[0].text} | ConvertTo-Json -Compress
""")
        self.assertTrue(report["error"])
        self.assertIn("CreateProcessWithLogonW failed: 1385", report["detail"])
        self.assertIn("exit code 1", report["detail"])
        self.assertLessEqual(len(report["detail"]), 1400)
        for private in ("fake-secret", "sk-test-secret", "private-token", "access_token=secret", "data:image"):
            self.assertNotIn(private, report["detail"])


if __name__ == "__main__":
    unittest.main()
