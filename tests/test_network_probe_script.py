"""Run the standalone Windows probe only against isolated local fixtures."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "scripts/windows/Test-CodexNetwork.cmd"
MARKER = ":__POWERSHELL__"
POWERSHELL = shutil.which("powershell.exe")


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, status, kind, body, *, server="Fixture"):
        self.send_response_only(status)
        self.send_header("Server", server)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", "must-not-be-recorded=secret")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("http://"):
            self.server.calls.append({"method": "GET", "path": self.path})
            self.reply(504, "text/html", b'<title>HIS Proxy Notification</title>proxy route rejected', server="netentsec_page_push")
            return
        body = (json.dumps({"profile": {"upstream": self.server.upstream}}).encode()
                if self.path == "/api/status" else b'{"app":"codex-packet-inspector","ok":true}')
        self.reply(200, "application/json", body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.calls.append({"bytes": len(body), "headers": dict(self.headers), "body": body})
        if self.path.startswith("/disconnect"):
            self.close_connection = True
            return
        if len(body) > 1_000_000:
            self.reply(403, "text/html", '<html><title>HIS Proxy Notification</title><p>该网站上传文件受限</p><p>申请上传小于1MB</p><p>申请上传大于1MB</p></html>'.encode(), server="netentsec_page_push")
        else:
            self.reply(401, "application/json", b'{"error":{"message":"Missing bearer authentication"}}')


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell is required")
class StandaloneNetworkProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex probe's ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
        self.environment.update(NO_PROXY="*", CODEX_NETWORK_TEST_NO_PAUSE="1")
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.environment.update(TEMP=str(self.scratch), TMP=str(self.scratch))
        self.payload = SOURCE.read_text(encoding="utf8").rsplit(MARKER, 1)[1]
        self.ps1 = self.root / "probe.ps1"
        self.ps1.write_text(self.payload, encoding="utf-8-sig")

    def gateway(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        server.daemon_threads = True
        server.calls, server.upstream = [], ""
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def run_ps(self, text):
        driver = self.root / "checks.ps1"
        driver.write_text(". $PSScriptRoot/probe.ps1 -LoadOnly\n" + text, encoding="utf-8-sig")
        result = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver)],
                                env=self.environment, capture_output=True, text=True, encoding="utf8", timeout=25)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_proxy_selection_error_classification_and_redaction(self):
        result = self.run_ps(r'''
function Assert($Value, $Message) { if (-not $Value) { throw $Message } }
$ctx = @{ map = @{ https = 'http://user:password@regional-proxy.example:8080'; http = 'regional-proxy.example:8080' }; source = 'environment'; bypass_mode = 'environment'; no_proxy = '127.0.0.1,localhost,[::1],.example.org'; windows_bypass = '' }
$route = Get-Route ([uri]'https://chatgpt.com/backend-api/codex/responses') $ctx
Assert ($route.endpoint -eq 'http://regional-proxy.example:8080/') 'Proxy credentials must be removed'
Assert ((Get-Route ([uri]'http://127.0.0.1:47844/health') $ctx).mode -eq 'direct') 'Loopback bypass'
Assert ((Get-Route ([uri]'http://[::1]:47844/health') $ctx).mode -eq 'direct') 'IPv6 loopback bypass'
Assert ((Get-Route ([uri]'https://sub.example.org/test') $ctx).mode -eq 'direct') 'NO_PROXY suffix'
$ctx.bypass_mode = 'windows'; $ctx.windows_bypass = '127.0.0.1*;<local>'
Assert ((Get-Route ([uri]'http://127.0.0.1:47844/health') $ctx).mode -eq 'direct') 'Registry wildcard bypass'
Assert ((Get-Route ([uri]'http://localhost:47844/health') $ctx).mode -eq 'direct') 'Registry local bypass'
$r = @{ status = 403; body = '<title>HIS Proxy Notification</title>该网站上传文件受限'; headers = @{ server = 'netentsec_page_push' }; bytes = 64000; written_bytes = 64000 }
Assert ((Get-ProbeVerdict $r $false).code -eq 'upload_blocked') 'Recognize actual HIS page'
$r.status = 401; $r.body = '<html>login</html>'; $r.headers = @{ 'content-type' = 'text/html' }
Assert ((Get-ProbeVerdict $r $false).code -eq 'unknown') 'An HTML login is not permission evidence'
$r.status = 403; $r.body = '{"error":"Forbidden"}'; $r.headers = @{ 'content-type' = 'application/json' }
Assert ((Get-ProbeVerdict $r $false).code -eq 'unknown') 'An API 403 does not prove size restrictions'
$r.status = 401; $r.body = '{"error":"Missing authentication"}'
Assert ((Get-ProbeVerdict $r $false).code -eq 'api_response') 'Expected unauthenticated API rejection'
$r.written_bytes = 0
Assert ((Get-ProbeVerdict $r $false).code -eq 'unknown') 'Do not certify a body that was not written'
$r.status = 407
Assert ((Get-ProbeVerdict $r $false).code -eq 'proxy_auth') 'Proxy authentication'
$r.status = 413
Assert ((Get-ProbeVerdict $r $false).code -eq 'size_blocked') 'Payload limit'
$safe = Protect-Text 'https://user:password@proxy.example:8080/a?token=secret Bearer abc123'
Assert ($safe -notmatch 'password|token=secret|abc123') 'Redact diagnostic text'
'PASS: proxy rules, classification, and credential redaction'
''')
        self.assertIn("PASS:", result)

    def test_proxy_is_really_used_even_for_loopback(self):
        proxy, relay = self.gateway(), self.gateway()
        result = self.run_ps(f'''
$ctx = @{{ map = @{{ http = 'http://127.0.0.1:{proxy.server_port}' }}; source = 'windows_manual'; bypass_mode = 'windows'; no_proxy = ''; windows_bypass = '' }}
$uri = [uri]'http://127.0.0.1:{relay.server_port}/health'
$route = Get-Route $uri $ctx
$result = Invoke-Probe 'local_proxy' $uri $route 0 2
if ($result.status -ne 504 -or $result.verdict -ne 'gateway_error') {{ throw ($result | ConvertTo-Json -Depth 8) }}
'PASS: local traffic reached the explicitly selected proxy'
''')
        self.assertIn("PASS:", result)
        self.assertEqual([r["method"] for r in proxy.calls], ["GET"])

    def test_double_click_launcher_exact_sizes_reports_and_no_inference(self):
        upstream, relay = self.gateway(), self.gateway()
        relay.upstream = f"http://127.0.0.1:{upstream.server_port}"
        report_dir = self.root / "report"
        curl_home = self.root / "curl-home"
        curl_home.mkdir()
        for name in (".curlrc", "_curlrc"):
            (curl_home / name).write_text('header = "Authorization: Bearer must-not-send"\n', encoding="utf8")
        self.environment["CURL_HOME"] = str(curl_home)
        # Exercise the actual one-file launcher, without external traffic.
        source = SOURCE.read_bytes().decode("utf8")
        source = source.replace("[int]$RelayPort = 47844", f"[int]$RelayPort = {relay.server_port}")
        source = source.replace("[string]$OutputDirectory = ''", "[string]$OutputDirectory = '" + str(report_dir).replace("'", "''") + "'")
        launcher = self.root / "Test CodexNetwork.cmd"
        launcher.write_bytes(source.encode("utf8"))
        result = subprocess.run(["cmd.exe", "/d", "/c", str(launcher)], env=self.environment,
                                capture_output=True, text=True, encoding="utf8", timeout=35)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads((report_dir / "report.json").read_text(encoding="utf8"))
        self.assertEqual([r["verdict"] for r in report["results"]],
                         ["local_ok", "local_ok", "api_response", "api_response", "upload_blocked"])
        self.assertEqual([c["bytes"] for c in upstream.calls], [64_000, 950_000, 1_100_000])
        for call in upstream.calls:
            with self.assertRaises(json.JSONDecodeError):
                json.loads(call["body"])
            headers = {k.lower(): v for k, v in call["headers"].items()}
            self.assertEqual(headers.get("content-encoding"), "identity")
            self.assertNotIn("authorization", headers)
            self.assertNotIn("cookie", headers)
        summary = (report_dir / "summary.txt").read_text(encoding="utf8")
        self.assertIn("1,100 kB 样本被网关限制", summary)
        full_error = (report_dir / "upload_1100000-response.txt").read_text(encoding="utf8")
        self.assertIn("该网站上传文件受限", full_error)
        self.assertIn("netentsec_page_push", full_error)
        self.assertNotIn("must-not-be-recorded", full_error)
        self.assertFalse(relay.calls, "Synthetic upload bodies must bypass the relay")
        self.assertFalse(list(self.scratch.glob("codex-network-test-*")), "Temporary probe files must be removed")

    def test_connection_failure_stops_following_uploads(self):
        upstream, relay = self.gateway(), self.gateway()
        report_dir = self.root / "failure-report"
        result = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.ps1),
                                 "-Target", f"http://127.0.0.1:{upstream.server_port}/disconnect/responses",
                                 "-RelayPort", str(relay.server_port), "-OutputDirectory", str(report_dir),
                                 "-TimeoutSeconds", "2"], env=self.environment, capture_output=True, text=True,
                                encoding="utf8", timeout=25)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads((report_dir / "report.json").read_text(encoding="utf8"))
        self.assertEqual([r["verdict"] for r in report["results"][-3:]], ["network_error", "skipped", "skipped"])
        self.assertEqual(len(upstream.calls), 1, "No automatic retry of a failed test")


if __name__ == "__main__":
    unittest.main()
