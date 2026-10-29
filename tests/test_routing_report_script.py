"""The standalone routing report must not send turns or export user contents."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/windows/Collect-CodexRouting.cmd"
POWERSHELL = shutil.which("powershell.exe")


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell required")
class RoutingReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex report's ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.payload = self.root / "collector.ps1"
        self.payload.write_text(SOURCE.read_text(encoding="utf8").rsplit(":__POWERSHELL__", 1)[1], encoding="utf-8-sig")
        self.environment = dict(os.environ, PYTHONIOENCODING="utf-8", CODEX_ROUTING_REPORT_NO_PAUSE="1")
        self.fake = self.root / "fake.py"
        self.fake.write_text(textwrap.dedent('''
            import json, sys, time
            for line in sys.stdin:
                request=json.loads(line)
                if 'id' not in request:continue
                method=request['method']
                if method=='initialize': result={'userAgent':'fixture'}
                elif method=='config/read':
                    result={'config':{'model_provider':'fixture','instructions':'PRIVATE_INSTRUCTIONS',
                        'model_providers':{'fixture':{'name':'Fixture','base_url':'http://127.0.0.1:47844/relay/PRIVATE_CAPABILITY/p/route',
                        'http_headers':{'Authorization':'PRIVATE_AUTH'}}}},
                        'origins':{'model_provider':{'name':{'type':'user','file':'C:/user/config.toml'},'version':'test'}},
                        'layers':[{'name':{'type':'sessionFlags'},'version':'v',
                        'config':{'model_provider':'fixture','mcp_servers':{'secret':'PRIVATE_MCP'}}}]}
                elif method=='thread/list':
                    assert request['params']['useStateDbOnly'] is True
                    result={'data':[{'id':'00000000-0000-7000-8000-000000000001','modelProvider':'fixture',
                        'cwd':'C:/test','preview':'PRIVATE_CONVERSATION', 'source':{'subAgent':{'prompt':'PRIVATE_PROMPT'}}}]}
                else: raise AssertionError('Unexpected RPC: '+method)
                print(json.dumps({'id':request['id'],'result':result}),flush=True)
        '''), encoding="utf8")

    def ps(self, text, *, timeout=20, environment=None):
        driver = self.root / "driver.ps1"
        driver.write_text(". $PSScriptRoot/collector.ps1 -LoadOnly\n" + text, encoding="utf-8-sig")
        result = subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver)],
                                capture_output=True, text=True, encoding="utf8", env=environment or self.environment, timeout=timeout)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    @staticmethod
    def literal(value):
        return "'" + str(value).replace("'", "''") + "'"

    def test_quote_roundtrip_and_process_argument_whitelist(self):
        self.ps(r'''
function Assert($v,$m){if(-not $v){throw $m}}
$values=@('C:\program files\codex.exe','app-server','-c','model_provider="fixture"','-c','model_providers.fixture.base_url="https://user:pass@api.example/x?secret=private"','-c','api_key="PRIVATE_KEY"','--profile','work','--cd','C:\directory with space\','PRIVATE_CONVERSATION')
$line=($values|ForEach-Object{Quote-NativeArgument $_}) -join ' '
$parsed=@(Split-NativeCommandLine $line)
Assert (($parsed|ConvertTo-Json -Compress) -eq ($values|ConvertTo-Json -Compress)) 'Argument quoting roundtrip'
$info=Get-ProcessRouting ([pscustomobject]@{CommandLine=$line;ProcessId=1;ParentProcessId=2;ExecutablePath=$values[0]})
$json=$info|ConvertTo-Json -Depth 8
Assert ($info.app_server -and $info.profile -eq 'work') 'Server/profile'
Assert ($info.routing_overrides.Count -eq 2) 'Only routing overrides'
Assert (-not ($json -match 'PRIVATE_KEY|PRIVATE_CONVERSATION|user:pass|secret=private')) 'Sensitive arguments excluded'
''')

    def test_protocol_filters_private_values_and_does_not_send_turns(self):
        self.ps("""
[Console]::InputEncoding=New-Object Text.UTF8Encoding($true)
$session=Start-ReadSession -Executable %s -Prefix @(%s)
try {
  [void](Invoke-ReadRpc $session 'initialize' @{clientInfo=@{name='test';version='1'}})
  $threads=Invoke-ReadRpc $session 'thread/list' @{limit=10;useStateDbOnly=$true}
  $config=Invoke-ReadRpc $session 'config/read' @{includeLayers=$true}
  $evidence=@{threads=@(Get-ThreadMetadata $threads.data[0]);configuration=(Get-ConfigEvidence $config 'C:/test')}
  $json=$evidence|ConvertTo-Json -Depth 30
  if($json -match 'PRIVATE_'){throw 'Private data leaked'}
  if($evidence.threads[0].modelProvider -ne 'fixture'){throw 'Provider missing'}
  if($evidence.configuration.origins.model_provider.source.type -ne 'user'){throw 'Origin missing'}
  if($evidence.configuration.layers[0].source.type -ne 'sessionFlags'){throw 'Layer missing'}
  $policy=Get-RoutingRequirements ([pscustomobject]@{modelProvider='forced';modelProviders=$config.config.model_providers;enforceResidency='us';instructions='PRIVATE_INSTRUCTIONS'})
  if($policy.modelProvider -ne 'forced'){throw 'Managed provider missing'}
  if(($policy|ConvertTo-Json -Depth 20) -match 'PRIVATE_'){throw 'Managed credentials leaked'}
  $denied=$false;try{[void](Invoke-ReadRpc $session 'turn/start' @{})}catch{$denied=$true}
  if(-not $denied){throw 'Turn requests must be prohibited'}
} finally {Stop-ReadSession $session}
""" % (self.literal(sys.executable), self.literal(self.fake)))

    def test_timeout_stops_only_owned_process_and_prevents_more_requests(self):
        self.fake.write_text("import time;time.sleep(15)\n", encoding="utf8")
        self.ps("""
$session=Start-ReadSession -Executable %s -Prefix @(%s)
try {
  try{[void](Invoke-ReadRpc $session 'initialize' @{} -TimeoutSeconds 1);throw 'Expected timeout'}catch{if($_.Exception.Message -notmatch 'timeout'){throw}}
  if(-not $session.broken){throw 'Timeout must invalidate session'}
  try{[void](Invoke-ReadRpc $session 'config/read' @{});throw 'Expected unavailable'}catch{if($_.Exception.Message -notmatch 'no longer available'){throw}}
} finally {Stop-ReadSession $session}
""" % (self.literal(sys.executable), self.literal(self.fake)))

    def test_partial_report_without_cli_is_saved_and_contains_no_user_text(self):
        destination = self.root / "report"
        self.ps("""
function Read-RelayStatus {throw 'Offline fixture'}
function Get-CimInstance {return @()}
function Get-Command {return $null}
$env:CODEX_CLI_PATH=''
$result=Invoke-RoutingCollection -Destination %s
if($result.errors.Count -ne 2){throw 'Expected relay and native errors'}
""" % self.literal(destination))
        report = json.loads((destination / "report.json").read_text(encoding="utf8"))
        self.assertFalse(report["session_flags_replayed"])
        self.assertEqual(report["threads"], [])
        self.assertIn("not attached", report["scope"])
        self.assertTrue((destination / "summary.txt").exists())
        self.assertRegex((destination / "summary.txt").read_text(encoding="utf8").splitlines()[1], r"^时间：\d{4}-")

    def test_cmd_wrapper_works_from_a_path_with_spaces_and_rejects_bad_ids(self):
        command = self.root / "Collect-CodexRouting.cmd"
        shutil.copyfile(SOURCE, command)
        result = subprocess.run([os.environ["COMSPEC"], "/d", "/c", str(command), "invalid-thread"],
                                capture_output=True, text=True, encoding="utf8", env=self.environment, timeout=10)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Invalid thread ID", result.stdout)
        self.assertFalse(list(self.root.glob("CodexConfigReport-*")))

    def test_installed_codex_reads_temporary_config_without_mutating_it(self):
        executable = os.environ.get("CODEX_CLI_PATH")
        if not executable or not Path(executable).is_file():
            self.skipTest("Installed Codex unavailable")
        codex_root = self.root / "native-home"
        codex_root.mkdir()
        config = codex_root / "config.toml"
        config.write_text('model_provider="fixture"\n[model_providers.fixture]\nname="Read-only fixture"\n'
                          'base_url="http://127.0.0.1:9/v1"\nwire_api="responses"\n'
                          '[analytics]\nenabled=false\n[features]\napps=false\nremote_plugin=false\n', encoding="utf8")
        original = config.read_bytes()
        destination = self.root / "native-report"

        class Status(BaseHTTPRequestHandler):
            def do_GET(handler):
                data=json.dumps({"version":"fixture","capture_enabled":True,"capture_errors":0,
                    "connection_state":"connected","storage":{"count":12},
                    "profile":{"config_path":str(config),"provider":"fixture","base_url":"http://127.0.0.1:47844/relay/PRIVATE_CAPABILITY/p/r"}}).encode()
                handler.send_response(200)
                handler.send_header("Content-Length",str(len(data)))
                handler.end_headers()
                handler.wfile.write(data)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Status)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.ps("""
function Get-CimInstance {return @()}
$env:CODEX_CLI_PATH=%s
$result=Invoke-RoutingCollection -Destination %s -Port %d
if($result.errors.Count){throw ($result.errors|ConvertTo-Json -Compress)}
if($result.configurations[0].config.model_provider -ne 'fixture'){throw 'Effective provider missing'}
if(-not $result.configurations[0].origins.model_provider){throw 'Provider origin missing'}
if($result.relay.request_count -ne 12){throw 'Relay status missing'}
if($result.threads.Count -ne 0){throw 'Unexpected native history'}
""" % (self.literal(executable), self.literal(destination), server.server_port), timeout=50)
        result_text = (destination / "report.json").read_text(encoding="utf8")
        self.assertNotIn("PRIVATE_CAPABILITY", result_text)
        self.assertEqual(json.loads(result_text)["codex_root"], str(codex_root))
        self.assertEqual(config.read_bytes(), original)
        self.assertFalse((codex_root / "auth.json").exists())
        self.assertFalse(any((codex_root / "sessions").rglob("*.jsonl")))


if __name__ == "__main__":
    unittest.main()
