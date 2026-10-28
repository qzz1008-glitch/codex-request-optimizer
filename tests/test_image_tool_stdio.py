"""Independent image tool installation, migration and stdio contract checks."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import tomlkit

from packet_inspector.config_link import ConfigLink
from packet_inspector.image_config import ImageToolConfig
from packet_inspector.image_tool_install import ImageToolInstallation, find_powershell


class ToolMigrationTests(unittest.TestCase):
    def setUp(self):
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
        self.assertTrue(data["tools"]["view_image"])
        self.assertNotIn("view_image", data["features"])
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
        self.assertTrue(data["tools"]["view_image"])
        self.assertNotIn("view_image", data["features"])

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

    def test_analysis_uses_a_fresh_codex_run_and_cleans_temp_files(self):
        image = self.root / "synthetic.png"
        image.write_bytes(b"synthetic-image-fixture")
        path = str(image).replace("'", "''")
        report = self.powershell("""
function Find-CodexCli { return 'fake-codex' }
function Invoke-ToolProcess($Executable, $Arguments, $TimeoutSeconds, $WorkingDirectory) {
    $script:ObservedWork = $WorkingDirectory
    if ($Arguments[0] -eq '--profile') {
        return @{ExitCode=0;Output='[{"name":"isolated_vision"},{"name":"other"}]'}
    }
    $script:ObservedArguments = $Arguments
    $result = $Arguments[[array]::IndexOf($Arguments,'--output-last-message')+1]
    [IO.File]::WriteAllText($result,'Visible error text',[Text.Encoding]::UTF8)
    return @{ExitCode=0;Output=''}
}
$value = Invoke-ImageAnalysis ([pscustomobject]@{path='""" + path + """';instruction='Read the error'}) '01a15d9a-f5e3-73c1-b4e7-87b3c5db9a3c'
$arguments = $script:ObservedArguments
@{answer=$value;ephemeral=('--ephemeral' -in $arguments);image=$arguments[[array]::IndexOf($arguments,'--image')+1];
  override=$arguments[[array]::IndexOf($arguments,'-c')+1];
  metadata=@($arguments | Where-Object {$_ -like 'responses_api_metadata.*'});
  cleaned=(-not(Test-Path -LiteralPath $script:ObservedWork))} | ConvertTo-Json -Compress
""")
        self.assertEqual(report["answer"], "Visible error text")
        self.assertTrue(report["ephemeral"])
        self.assertEqual(Path(report["image"]).resolve(), image.resolve())
        self.assertTrue(report["cleaned"])
        self.assertIn('"isolated_vision"={enabled=false}', report["override"])
        self.assertIn('"other"={enabled=false}', report["override"])
        self.assertEqual(report["metadata"], [
            'responses_api_metadata.codex_inspector_request_kind="image_analysis"',
            'responses_api_metadata.codex_inspector_parent_thread_id="01a15d9a-f5e3-73c1-b4e7-87b3c5db9a3c"'])

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


if __name__ == "__main__":
    unittest.main()
