import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from packet_inspector.app_icon import APP_ICON
from packet_inspector.installation import manage_installation


class InstallationRefreshTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows startup action")
    def test_login_task_migrates_to_the_current_version_without_stopping_any_process(self):
        source = Path(__file__).resolve().parents[1] / "packet_inspector" / "windows_install.ps1"
        script = r'''
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$ErrorActionPreference='Stop'
$env:USERPROFILE=$env:TEST_INSTALL_ROOT
$data=Join-Path $env:USERPROFILE '.codex-packet-inspector'
$canonical=Join-Path $data 'bin\CodexPacketInspector.exe'
$launcher=Join-Path $data 'bin\versions\current\CodexPacketInspector.exe'
New-Item -ItemType Directory -Path (Split-Path $launcher) -Force|Out-Null
[IO.File]::WriteAllText($canonical,'entry remains running')
[IO.File]::WriteAllText($launcher,'current version')
$global:testTask=[pscustomobject]@{State='Ready';Actions=@([pscustomobject]@{Execute=$launcher})}
function Get-ScheduledTask { $global:testTask }
function New-ScheduledTaskAction { param($Execute,$Argument,$WorkingDirectory) [pscustomobject]@{Execute=$Execute;Arguments=$Argument;WorkingDirectory=$WorkingDirectory} }
function New-ScheduledTaskTrigger { 'test-trigger' }
function New-ScheduledTaskPrincipal { 'test-principal' }
function New-ScheduledTaskSettingsSet { 'test-settings' }
function Register-ScheduledTask { param($Action) $global:testAction=$Action }
function Stop-Process { throw 'must not stop a process' }
function Stop-ScheduledTask { throw 'must not stop the login task' }
$source=[IO.File]::ReadAllText($env:TEST_INSTALL_SOURCE).Replace("[Environment]::GetFolderPath('Programs')",'$env:TEST_INSTALL_ROOT')
$status=& ([scriptblock]::Create($source)) -Action EnableStartup -Executable $canonical -LauncherExecutable $launcher -RouterLaunch -DataDirectory $data -ConfigPath (Join-Path $env:USERPROFILE 'config.toml')
@{action=$global:testAction;status=($status|ConvertFrom-Json);canonical=[IO.File]::ReadAllText($canonical)}|ConvertTo-Json -Depth 4 -Compress
'''
        with tempfile.TemporaryDirectory() as folder:
            program = Path(folder) / "startup-check.ps1"
            program.write_text(script, encoding="utf8")
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(program)],
                                    env={**os.environ, "TEST_INSTALL_SOURCE": str(source), "TEST_INSTALL_ROOT": str(Path(folder).resolve())},
                                    capture_output=True, text=True, encoding="utf8", errors="replace", timeout=20,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads(result.stdout)
        self.assertIn("versions\\current\\CodexPacketInspector.exe", actual["action"]["Execute"])
        self.assertTrue(actual["action"]["Arguments"].startswith("--router --background --tray "))
        self.assertTrue(actual["status"]["login_start"])
        self.assertEqual(actual["canonical"], "entry remains running")

    def test_each_worker_refresh_preserves_the_existing_login_startup_state(self):
        from packet_inspector.server import InspectorServer
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "config.toml"
            config.write_text('model="test"', encoding="utf8")
            server = InspectorServer(("127.0.0.1", 0), root / "data", config, worker_id="1" * 32)
            try:
                for enabled in [False, True]:
                    with self.subTest(enabled=enabled), \
                            patch("packet_inspector.server.startup_supported", return_value=True), \
                            patch("packet_inspector.server.manage_installation", return_value={"login_start": enabled}) as install, \
                            patch("packet_inspector.server.configure_startup") as configure:
                        server.worker_control({"action": "installation_refresh"})
                        self.assertEqual([call.args[0] for call in install.call_args_list], ["Status", "Install"])
                        self.assertEqual(server.settings["start_on_login"], enabled)
                        if enabled:
                            configure.assert_called_once()
                        else:
                            configure.assert_not_called()
            finally:
                server.server_close()

    @unittest.skipUnless(os.name == "nt", "Windows installation integration")
    def test_upgrade_uses_the_current_launcher_and_preserves_the_live_entry_binary(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            data = root / ".codex-packet-inspector"
            canonical = data / "bin" / "CodexPacketInspector.exe"
            canonical.parent.mkdir(parents=True)
            canonical.write_bytes(b"entry")
            executable = canonical.parent / "versions" / ("a" * 64) / canonical.name
            with patch("packet_inspector.installation.Path.home", return_value=root), \
                    patch.object(sys, "frozen", True, create=True), \
                    patch.object(sys, "executable", str(executable)), \
                    patch("packet_inspector.installation.subprocess.run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = '{"installed":true}'
                manage_installation("Install", data, root / "config.toml", 47844)
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("-Executable") + 1], str(canonical))
            self.assertEqual(command[command.index("-LauncherExecutable") + 1], str(executable))
            self.assertIn("-RouterLaunch", command)
            self.assertEqual(command[command.index("-IconPath") + 1], str(APP_ICON))
            self.assertEqual(canonical.read_bytes(), b"entry")

    @unittest.skipUnless(os.name == "nt", "Windows shortcut properties")
    def test_owned_pins_are_refreshed_and_other_programs_and_profiles_are_preserved(self):
        source = Path(__file__).resolve().parents[1] / "packet_inspector" / "windows_install.ps1"
        script = r'''
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$ErrorActionPreference='Stop'
$tokens=$null;$parseErrors=$null
$tree=[Management.Automation.Language.Parser]::ParseFile($env:TEST_INSTALL_SOURCE,[ref]$tokens,[ref]$parseErrors)
if($parseErrors.Count){throw $parseErrors[0]}
$compile=$tree.Find({param($node) $node -is [Management.Automation.Language.CommandAst] -and $node.GetCommandName() -eq 'Add-Type'},$true)
Invoke-Expression $compile.Extent.Text
if(-not [CodexRequestOptimizer.ShellRefresh].GetMethod('SHChangeNotify')){throw 'Shell refresh entry point is missing'}
foreach($name in @('Update-OwnedOptimizerShortcut','Get-OptimizerFileHash')) {
 $definition=$tree.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name},$true)
 . ([scriptblock]::Create($definition.Extent.Text))
}
$root=$env:TEST_INSTALL_ROOT
$data=Join-Path $root 'data';$bin=Join-Path $data 'bin';$config=Join-Path $root 'config.toml';$icon=Join-Path $bin 'request-optimizer-logo.ico'
$canonical=Join-Path $bin 'CodexPacketInspector.exe';$old=Join-Path $bin 'versions\old\CodexPacketInspector.exe'
$shell=New-Object -ComObject WScript.Shell
$results=@()
foreach($kind in @('canonical','versioned','foreign','profile','router')){
 $path=Join-Path $root ($kind+'.lnk');$link=$shell.CreateShortcut($path)
 $link.TargetPath=if($kind -eq 'foreign'){Join-Path $root 'different.exe'}elseif($kind -eq 'canonical'){$canonical}else{$old}
 $profile=if($kind -eq 'profile'){Join-Path $root 'other-data'}else{$data}
 $link.Arguments='--data-dir "'+$profile+'" --config "'+$config+'"';$link.Save()
 $before=Get-OptimizerFileHash -Path $path
 $options=@{}
 if($kind -eq 'router'){$options=@{LauncherExecutable=(Join-Path $bin 'versions\current\CodexPacketInspector.exe');RouterLaunch=$true}}
 $changed=Update-OwnedOptimizerShortcut -Path $path -BinDirectory $bin -DataDirectory $data -ConfigPath $config -IconPath $icon @options
 $after=$shell.CreateShortcut($path)
 $results+=@{kind=$kind;changed=$changed;canonical=($after.TargetPath -eq $canonical);router=($after.Arguments.StartsWith('--router '));current_icon=($after.IconLocation -eq ($icon+',0'));preserved=($before -eq (Get-OptimizerFileHash -Path $path));target=$after.TargetPath}
}
$results|ConvertTo-Json -Compress
'''
        with tempfile.TemporaryDirectory() as folder:
            program = Path(folder) / "check.ps1"
            program.write_text(script, encoding="utf8")
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(program)],
                                    env={**os.environ, "TEST_INSTALL_SOURCE": str(source), "TEST_INSTALL_ROOT": str(Path(folder).resolve())},
                                    capture_output=True, text=True, encoding="utf8", errors="replace", timeout=20,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        cases = {case["kind"]: case for case in json.loads(result.stdout)}
        for kind in ["canonical", "versioned"]:
            self.assertTrue(cases[kind]["changed"])
            self.assertTrue(cases[kind]["canonical"])
            self.assertTrue(cases[kind]["current_icon"])
        for kind in ["foreign", "profile"]:
            self.assertFalse(cases[kind]["changed"])
            self.assertTrue(cases[kind]["preserved"])
        self.assertTrue(cases["router"]["changed"])
        self.assertTrue(cases["router"]["router"])
        self.assertIn("versions\\current\\CodexPacketInspector.exe", cases["router"]["target"])
