"""Run the standalone Windows probe only against isolated local fixtures."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
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

    def do_CONNECT(self):
        self.server.calls.append({"method": "CONNECT", "path": self.path})
        if getattr(self.server, "hold_connect", False):
            self.server.release_connect.wait(5)
            self.close_connection = True
            return
        status = getattr(self.server, "connect_status", 403)
        self.reply(status, "text/html", b'' if status == 200 else b'<title>HIS Proxy Notification</title>upload blocked',
                   server="netentsec_page_push")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.calls.append({"bytes": len(body), "headers": dict(self.headers), "body": body})
        if self.path.startswith("/disconnect"):
            self.close_connection = True
            return
        if self.path.startswith("/forbidden"):
            self.reply(403, "application/json", b'{"error":"Forbidden"}')
            return
        if len(body) > getattr(self.server, "upload_limit_bytes", 1_000_000):
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

    def test_schannel_compatibility_and_precise_tls_diagnosis(self):
        result = self.run_ps(r'''
function Assert($Value, $Message) { if (-not $Value) { throw $Message } }
$https = [uri]'https://chatgpt.com/backend-api/codex/responses'
$options = @(Get-CurlTlsArguments 'curl 8.16.0 (Windows) libcurl/8.16.0 Schannel zlib/1.3.1' $https)
Assert ($options.Count -eq 1 -and $options[0] -eq '--ssl-revoke-best-effort') 'Only unavailable revocation checks may be tolerated'
Assert (@(Get-CurlTlsArguments 'curl 7.70.0 Schannel' $https).Count -eq 1) 'First supported curl version'
Assert (@(Get-CurlTlsArguments 'curl 7.69.0 Schannel' $https).Count -eq 0) 'Older curl must not receive an unsupported option'
Assert (@(Get-CurlTlsArguments 'curl 8.16.0 OpenSSL/3.0.0' $https).Count -eq 0) 'Other TLS backends keep their defaults'
Assert (@(Get-CurlTlsArguments 'curl 8.16.0 Schannel' ([uri]'http://127.0.0.1/health')).Count -eq 0) 'Plain HTTP needs no TLS options'
$r = @{ status = 0; body = ''; headers = @{}; bytes = 64000; written_bytes = 0; curl_exit = 35; exceptions = @(@{type='curl'}); transport_detail = 'schannel: CRYPT_E_NO_REVOCATION_CHECK (0x80092012)' }
Assert ((Get-ProbeVerdict $r $false).code -eq 'tls_revocation_error') 'Classify the real failure separately from local connectivity'
$r.transport_detail = 'schannel: 0x80092013'
Assert ((Get-ProbeVerdict $r $false).code -eq 'tls_revocation_error') 'Offline revocation check'
$r.transport_detail = 'schannel: CERT_E_REVOKED (0x800B010C)'; $r.curl_exit = 60
Assert ((Get-ProbeVerdict $r $false).code -eq 'tls_error') 'A revoked certificate is a certificate error, not missing revocation data'
$r.transport_detail = 'schannel: CERT_E_UNTRUSTEDROOT (0x800B0109)'
Assert ((Get-ProbeVerdict $r $false).code -eq 'tls_error') 'Untrusted certificate errors remain visible'
$r.transport_detail = ''; $r.curl_exit = 6
Assert ((Get-ProbeVerdict $r $false).code -eq 'dns_error') 'DNS failure is distinguishable'
$r.curl_exit = 28
Assert ((Get-ProbeVerdict $r $false).code -eq 'timeout') 'Timeout is distinguishable'
'PASS: TLS settings preserve verification and failures identify their stage'
''')
        self.assertIn("PASS:", result)

    def test_connect_failures_are_distinct_from_post_upload_failures(self):
        result = self.run_ps(r'''
function Assert($Value, $Message) { if (-not $Value) { throw $Message } }
$r = @{status=0;headers=@{};body='';bytes=20000;written_bytes=0;curl_exit=28;
    exceptions=@(@{type='curl'});flow=@{http_connect='000'};
    transport_detail="> CONNECT chatgpt.com:443 HTTP/1.1`r`n* Connection timed out after 5015 milliseconds"}
$v = Get-ProbeVerdict $r $false
Assert ($v.code -eq 'timeout' -and $v.failure_stage -eq 'proxy_connect' -and $v.message -match '正文未发送') 'CONNECT timeout is not a payload-size result'
$r.flow.http_connect = '200'
Assert (-not (Get-ProbeVerdict $r $false).failure_stage) 'A timeout after CONNECT success must not be blamed on the tunnel'
$r.flow.http_connect = '403'; $r.status = 403; $r.curl_exit = 56
$r.body = '<title>HIS Proxy Notification</title>该网站上传文件受限'
Assert ((Get-ProbeVerdict $r $false).code -eq 'gateway_error') 'CONNECT rejection cannot establish a body-size limit, even with an upload block page'
$r.flow.http_connect = '200'
Assert ((Get-ProbeVerdict $r $false).code -eq 'upload_blocked') 'A real POST may be rejected by Content-Length before the body is sent'
'PASS: tunnel failures and actual POST size blocks stay distinct'
''')
        self.assertIn("PASS:", result)

    def test_scan_reports_no_upload_measurement_when_connect_fails(self):
        for held in (False, True):
            with self.subTest(timeout=held):
                proxy, relay = self.gateway(), self.gateway()
                proxy.hold_connect = held
                proxy.release_connect = threading.Event()
                self.addCleanup(proxy.release_connect.set)
                report_dir = self.root / ("connect-timeout" if held else "connect-rejected")
                environment = dict(self.environment, HTTPS_PROXY=f"http://127.0.0.1:{proxy.server_port}",
                                   NO_PROXY="127.0.0.1,localhost", CODEX_NETWORK_TEST_SCRIPT=str(SOURCE))
                result = subprocess.run(
                    [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.ps1),
                     "-Target", "https://fixture.invalid/responses", "-RelayPort", str(relay.server_port),
                     "-OutputDirectory", str(report_dir), "-TimeoutSeconds", "1", "-FindUploadLimit"],
                    env=environment, capture_output=True, text=True, encoding="utf8", timeout=25)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                report = json.loads((report_dir / "report.json").read_text(encoding="utf8"))
                rows = [r for r in report["results"] if r["key"].startswith("upload_")]
                self.assertEqual(rows[0]["verdict"], "timeout" if held else "gateway_error")
                self.assertEqual(rows[0]["failure_stage"], "proxy_connect")
                self.assertEqual(rows[0]["written_bytes"], 0)
                self.assertTrue(all(r["verdict"] == "skipped" for r in rows[1:]))
                self.assertEqual(proxy.calls, [{"method": "CONNECT", "path": "fixture.invalid:443"}])
                self.assertIn("未测得上传大小", report["upload_limit"]["message"])
                self.assertIn("实际上传 0/20000 B", result.stdout)
                self.assertEqual(report["mode"], "upload_limit")
                self.assertEqual(report["script_sha256"], hashlib.sha256(SOURCE.read_bytes()).hexdigest())
                self.assertEqual(report["target_source"], "explicit_target")
                self.assertIn("curl", report["runtime"]["curl_details"])
                self.assertTrue(Path(report["runtime"]["curl_path"]).is_file())
                self.assertIn("interfaces", report["network"])
                self.assertEqual(rows[0]["route"]["reason"], "proxy_for_https")
                self.assertEqual(rows[0]["timeouts"]["connect_seconds"], 5)
                self.assertEqual(rows[0]["timeouts"]["total_seconds"], 1)
                self.assertTrue(rows[0]["progress"]["tcp_confirmed"])
                self.assertTrue(rows[0]["progress"]["connect_sent"])
                self.assertFalse(rows[0]["progress"]["tls_confirmed"])
                self.assertFalse(rows[0]["progress"]["request_headers_sent"])
                self.assertEqual(rows[0]["progress"]["upload"], "not_sent")
                self.assertEqual(rows[0]["progress"]["connect_status"], None if held else 403)
                self.assertIn("--trace-time", rows[0]["curl_arguments"])
                self.assertNotIn("--insecure", rows[0]["curl_arguments"])
                self.assertRegex(rows[0]["transport_detail"], r"(?m)^\d{2}:\d{2}:\d{2}\.\d+ > CONNECT ")
                self.assertTrue(rows[0]["started"] < rows[0]["finished"])
                self.assertEqual(report["address_probe"]["scope"], "independent_dns_tcp_probe")
                self.assertTrue(report["address_probe"]["local_endpoint"].startswith("127.0.0.1:"))
                detail = (report_dir / "upload_20000-response.txt").read_text(encoding="utf8")
                self.assertRegex(detail, r'"failure_stage"\s*:\s*"proxy_connect"')
                self.assertIn("Timestamped curl transport", detail)
                self.assertEqual(list(self.scratch.iterdir()), [])
                proxy.release_connect.set()

    def test_tunnel_success_and_tls_failure_remain_separate_in_diagnostics(self):
        proxy = self.gateway()
        proxy.connect_status = 200
        result = self.run_ps(f'''
$route = @{{mode='proxy';source='fixture';endpoint='http://127.0.0.1:{proxy.server_port}';bypass=$false}}
$r = Invoke-Probe 'tls_failure' ([uri]'https://fixture.invalid/responses') $route 20000 2
if ($r.progress.connect_status -ne 200 -or -not $r.progress.tcp_confirmed -or $r.progress.tls_confirmed -or $r.progress.request_headers_sent) {{
    throw ($r | ConvertTo-Json -Depth 8)
}}
if ($r.curl_exit -eq 0 -or -not $r.transport_detail -or $r.failure_stage -eq 'proxy_connect') {{ throw 'TLS failure evidence lost or misattributed' }}
'PASS: CONNECT succeeded while origin TLS did not complete'
''')
        self.assertIn("PASS:", result)

    def test_large_native_trace_keeps_final_error_and_redacts_credentials(self):
        result = self.run_ps(r'''
$path = Join-Path $PSScriptRoot 'large-trace.txt'
foreach ($encoding in @([Text.Encoding]::Default, [Text.Encoding]::UTF8)) {
    $message = '无法检查证书是否已吊销 / certificate status café'
    $expected = $encoding.GetString($encoding.GetBytes($message))
    $text = "11:33:39.000000 > CONNECT fixture.invalid:443 HTTP/1.1`r`n" +
        ("11:33:39.001000 * verbose TLS certificate line`r`n" * 4000) +
        "11:33:44.000000 * $message`r`nProxy-Authenticate: Negotiate private-challenge`r`nAuthorization: Bearer private-token`r`n"
    [IO.File]::WriteAllBytes($path, $encoding.GetBytes($text))
    $decoded = Read-LimitedText $path -NativeOutput -PreserveTail
    if (-not $decoded.Contains('> CONNECT ') -or -not $decoded.Contains($expected) -or -not $decoded.Contains('diagnostic truncated')) { throw 'Beginning or final error was lost' }
    if ($decoded.Contains('private-challenge') -or $decoded.Contains('private-token')) { throw 'Credentials leaked into transport log' }
    if ([Text.Encoding]::UTF8.GetByteCount($decoded) -gt ($script:ResponseLimit + 1024)) { throw 'Transport retention exceeded its budget' }
}
'PASS: large native traces retain both ends within a fixed budget'
''')
        self.assertIn("PASS:", result)

    def test_script_errors_keep_their_location_and_completed_local_probe(self):
        output = self.run_ps(r'''
$r = Invoke-Probe 'invalid_body' ([uri]'https://fixture.invalid/responses') @{mode='direct';endpoint='';bypass=$true} 1 1
if (-not $r.script_error.id -or -not $r.script_error.stack -or $r.script_error.position -notmatch 'Test body is too small') { throw 'Per-probe script error location missing' }
'PASS: unexpected script failures retain their location'
''')
        self.assertIn("PASS:", output)
        relay = self.gateway()
        directory = self.root / "fatal-report"
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.ps1),
             "-Target", f"http://127.0.0.1:{relay.server_port}/responses", "-RelayPort", str(relay.server_port),
             "-OutputDirectory", str(directory), "-FindUploadLimit"],
            env=self.environment, capture_output=True, text=True, encoding="utf8", timeout=25)
        self.assertEqual(result.returncode, 1)
        failure = json.loads((directory / "fatal-error.txt").read_text(encoding="utf8"))
        self.assertIn("Refusing to upload test bodies to the relay itself", failure["exceptions"][0]["message"])
        self.assertTrue(failure["script_error"]["id"])
        self.assertTrue(failure["script_error"]["stack"])
        self.assertIn("probe.ps1", failure["script_error"]["position"])
        self.assertTrue((directory / "local_direct-response.txt").is_file())
        self.assertFalse(relay.calls)

    def test_native_diagnostic_encoding_and_redaction(self):
        result = self.run_ps(r'''
$path = Join-Path $PSScriptRoot 'native-error.txt'
$message = '无法检查证书是否已吊销 / certificate status café'
foreach ($encoding in @([Text.Encoding]::Default, [Text.Encoding]::UTF8)) {
    # ANSI cannot represent every language. Compare against what this code page
    # can write; UTF-8 separately checks that the complete Chinese text survives.
    # The accented text also exercises ANSI fallback on English Windows.
    $expected = $encoding.GetString($encoding.GetBytes($message))
    [IO.File]::WriteAllBytes($path, $encoding.GetBytes($message + ' Authorization: Bearer must-not-keep'))
    $decoded = Read-LimitedText $path -NativeOutput
    if (-not $decoded.Contains($expected) -or $decoded.Contains('must-not-keep')) { throw ('Native diagnostic decoding/redaction failed for code page ' + $encoding.CodePage) }
}
'PASS: localized diagnostics remain readable and redacted'
''')
        self.assertIn("PASS:", result)

    def test_permission_and_proxy_conclusions_identify_actual_outcomes(self):
        result = self.run_ps(r'''
function Assert($Value, $Message) { if (-not $Value) { throw $Message } }
function Sample([int]$Size) {
    return @{ key = 'upload_' + $Size; bytes = $Size; written_bytes = $Size; verdict = 'api_response'; status = 401; message = 'API response';
        route = @{ mode = 'proxy' }; flow = @{ http_connect = '200'; http_code = '401'; time_appconnect = '0.125000' } }
}
$rows = @(@{key='local_direct';verdict='local_ok'}, @{key='local_proxy';verdict='local_ok'}, (Sample 64000), (Sample 950000), (Sample 1100000))
$rows[-1].verdict = 'upload_blocked'; $rows[-1].status = 403; $rows[-1].message = '企业网关限制上传'
$conclusions = @(Get-NetworkConclusions $rows @{mode='proxy'} @{tcp='connected'})
Assert ($conclusions.Count -eq 4) 'Always report local, proxy, below-limit and above-limit conclusions'
Assert ($conclusions[0].status -eq 'ok') 'Both local checks passed'
Assert ($conclusions[1].status -eq 'ok') 'A proxied HTTPS response verifies the proxy, independently of upload permissions'
Assert ($conclusions[2].status -eq 'allowed' -and $conclusions[2].result -eq '具备') 'Both small samples passed'
Assert ($conclusions[3].status -eq 'blocked' -and $conclusions[3].result -eq '不具备') 'Large upload was explicitly blocked'
$rows[-1].verdict = 'api_response'; $rows[-1].status = 401
Assert ((Get-UploadConclusion $rows @(1100000)).status -eq 'allowed') 'A completed large sample can pass'
Assert (@(Get-NetworkConclusions $rows @{mode='direct'} @{tcp='connected'})[1].status -eq 'not_used') 'Direct access does not certify a proxy'
$rows[2].verdict = 'tls_error'; $rows[2].message = 'TLS 握手失败'; $rows[2].written_bytes = 0; $rows[2].flow = @{http_connect='200';http_code='000';time_appconnect='0.000000'}
$rows[3].verdict = 'skipped'; $rows[3].flow = @{}; $rows[4].verdict = 'skipped'; $rows[4].flow = @{}
$conclusions = @(Get-NetworkConclusions $rows @{mode='proxy'} @{tcp='connected'})
Assert ($conclusions[1].status -eq 'unknown' -and $conclusions[1].detail.Contains('DNS/TCP 正常')) 'TCP success alone does not prove HTTPS access'
Assert ($conclusions[2].status -eq 'unknown' -and $conclusions[3].status -eq 'unknown') 'TLS failures cannot be reported as denied permissions'
$conclusions = @(Get-NetworkConclusions $rows @{mode='proxy'} @{})
Assert ($conclusions[1].status -eq 'failed') 'An unreachable proxy is clearly abnormal'
$rows[2].verdict = 'proxy_auth'
Assert (@(Get-NetworkConclusions $rows @{mode='proxy'} @{tcp='connected'})[1].status -eq 'failed') 'Proxy authentication failure is not normal connectivity'
'PASS: conclusions distinguish proxy transport from upload permissions'
''')
        self.assertIn("PASS:", result)

    def test_permission_conclusions_require_complete_relevant_samples(self):
        result = self.run_ps(r'''
function Assert($Value, $Message) { if (-not $Value) { throw $Message } }
$small = @{key='upload_64000';bytes=64000;written_bytes=64000;verdict='api_response';status=401}
$near = @{key='upload_950000';bytes=950000;written_bytes=950000;verdict='api_response';status=401}
Assert ((Get-UploadConclusion @($small) @(64000,950000)).status -eq 'unknown') 'Missing the near-limit sample must not certify below-limit access'
$near.written_bytes = 0
Assert ((Get-UploadConclusion @($small,$near) @(64000,950000)).status -eq 'unknown') 'Early rejection without a full upload is inconclusive'
$near.written_bytes = 950000; $near.verdict = 'unknown'; $near.status = 403; $near.message = 'Unknown API rejection'
Assert ((Get-UploadConclusion @($small,$near) @(64000,950000)).status -eq 'unknown') 'A generic 403 does not prove upload permission denial'
$near.verdict = 'size_blocked'; $near.status = 413; $near.message = '请求大小被限制'
Assert ((Get-UploadConclusion @($small,$near) @(64000,950000)).status -eq 'blocked') 'An explicit near-limit size rejection means below-limit access is restricted'
'PASS: incomplete samples and unrelated errors remain inconclusive'
''')
        self.assertIn("PASS:", result)

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
        for row, call in zip(report["results"][2:], upstream.calls):
            self.assertEqual(row["request_sha256"], hashlib.sha256(call["body"]).hexdigest())
            self.assertEqual(row["progress"]["upload"], "complete")
            self.assertTrue(row["progress"]["request_headers_sent"])
            self.assertIn("local_ip", row["flow"])
            self.assertIn("time_pretransfer", row["flow"])
            self.assertIn("[redacted]", row["response_header_text"])
            self.assertNotIn("must-not-be-recorded", row["response_header_text"])
        for call in upstream.calls:
            with self.assertRaises(json.JSONDecodeError):
                json.loads(call["body"])
            headers = {k.lower(): v for k, v in call["headers"].items()}
            self.assertEqual(headers.get("content-encoding"), "identity")
            self.assertNotIn("authorization", headers)
            self.assertNotIn("cookie", headers)
        summary = (report_dir / "summary.txt").read_text(encoding="utf8")
        self.assertIn("本机中转：正常", summary)
        self.assertIn("代理：未使用", summary)
        self.assertIn("1 MB 以下上传权限：具备", summary)
        self.assertIn("1 MB 以上上传权限：不具备", summary)
        self.assertIn("1,100 kB 样本被限制（HTTP 403）", summary)
        self.assertEqual([c["status"] for c in report["conclusions"]], ["ok", "not_used", "allowed", "blocked"])
        self.assertIn("[3/6] 检查上游直连连通性（DNS / TCP）", result.stdout)
        first_upload = result.stdout.index("[4/6]")
        self.assertEqual(result.stdout[:first_upload].count("HTTP 200 · 本机入口正常"), 2,
                         "Both local checks must report success before upload probing")
        full_error = (report_dir / "upload_1100000-response.txt").read_text(encoding="utf8")
        self.assertIn("该网站上传文件受限", full_error)
        self.assertIn("netentsec_page_push", full_error)
        self.assertNotIn("must-not-be-recorded", full_error)
        self.assertFalse(relay.calls, "Synthetic upload bodies must bypass the relay")
        self.assertFalse(list(self.scratch.glob("codex-network-test-*")), "Temporary probe files must be removed")

    def test_upload_scan_summary_keeps_uncertain_and_incomplete_samples_distinct(self):
        result = self.run_ps(r'''
function Assert($Value, $Message) { if (-not $Value) { throw $Message } }
function Sample([int]$Size, [string]$Verdict) {
    return @{key=('upload_' + $Size);bytes=$Size;written_bytes=$Size;verdict=$Verdict;message='fixture'}
}
$rows = @(1..50 | ForEach-Object { Sample ($_ * 20000) 'api_response' })
$state = Get-UploadScanSummary $rows
Assert ($state.status -eq 'at_least' -and $state.samples -eq 50 -and $state.step_bytes -eq 20000 -and $state.max_test_bytes -eq 1000000) 'All 50 samples cover the fixed range'
Assert ($null -eq $state.smallest_blocked_bytes) 'A passed ceiling is only a lower bound'
$rows[0] = Sample 20000 'upload_blocked'
Assert ((Get-UploadScanSummary $rows).status -eq 'inconclusive') 'Inconsistent size decisions are not a size limit'
$rows[0] = Sample 20000 'api_response'; $rows[0].written_bytes = 1
Assert ((Get-UploadScanSummary $rows).status -eq 'inconclusive') 'Partial writes cannot establish permission'
$rows = @(1..50 | ForEach-Object { Sample ($_ * 20000) 'upload_blocked' })
Assert ((Get-UploadScanSummary $rows).status -eq 'no_passing_sample') 'All blocked does not mean a zero-byte limit'
$rows = @(1..50 | ForEach-Object { Sample ($_ * 20000) 'skipped' })
$rows[0] = Sample 20000 'network_error'
$state = Get-UploadScanSummary $rows
Assert ($state.status -eq 'incomplete' -and $state.samples -eq 1 -and $state.skipped_samples -eq 49) 'Skipped samples remain explicitly untested'
'PASS: fixed scan summaries preserve uncertain and incomplete outcomes'
''')
        self.assertIn("PASS:", result)

    def test_upload_limit_launcher_scans_all_fifty_sizes_even_after_a_size_block(self):
        upstream, relay = self.gateway(), self.gateway()
        upstream.upload_limit_bytes = 550_000
        relay.upstream = f"http://127.0.0.1:{upstream.server_port}"
        report_dir = self.root / "limit-report"
        source = SOURCE.read_text(encoding="utf8")
        source = source.replace("[int]$RelayPort = 47844", f"[int]$RelayPort = {relay.server_port}")
        source = source.replace("[string]$OutputDirectory = ''", "[string]$OutputDirectory = '" + str(report_dir).replace("'", "''") + "'")
        launcher = self.root / "Test Upload Limit.cmd"
        launcher.write_bytes(source.encode("utf8"))
        result = subprocess.run(["cmd.exe", "/d", "/c", str(launcher), "--upload-limit"], env=self.environment,
                                capture_output=True, text=True, encoding="utf8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads((report_dir / "report.json").read_text(encoding="utf8"))
        limit = report["upload_limit"]
        self.assertEqual(limit["status"], "bounded")
        self.assertEqual(limit["largest_unblocked_bytes"], 540_000)
        self.assertEqual(limit["smallest_blocked_bytes"], 560_000)
        self.assertEqual(limit["step_bytes"], 20_000)
        self.assertEqual(limit["max_test_bytes"], 1_000_000)
        self.assertEqual([call["bytes"] for call in upstream.calls], list(range(20_000, 1_000_001, 20_000)))
        for call in upstream.calls:
            self.assertNotIn("authorization", {key.lower() for key in call["headers"]})
            with self.assertRaises(json.JSONDecodeError):
                json.loads(call["body"])
        summary = (report_dir / "summary.txt").read_text(encoding="utf8")
        self.assertIn("最大未拦截样本", summary)
        self.assertIn("实际上传 20000/20000 B", summary)
        self.assertEqual(len(report["conclusions"]), 2, "Fixed legacy permission samples are not relevant to the range scan")
        self.assertFalse(relay.calls)
        self.assertFalse(list(self.scratch.glob("codex-network-test-*")))

    def test_scan_includes_one_megabyte_but_never_uploads_more(self):
        upstream, relay = self.gateway(), self.gateway()
        upstream.upload_limit_bytes = 10_000_000
        report_dir = self.root / "ceiling-report"
        result = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.ps1),
                                 "-Target", f"http://127.0.0.1:{upstream.server_port}/responses",
                                 "-RelayPort", str(relay.server_port), "-OutputDirectory", str(report_dir),
                                 "-FindUploadLimit"],
                                env=self.environment, capture_output=True, text=True, encoding="utf8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads((report_dir / "report.json").read_text(encoding="utf8"))
        self.assertEqual(report["upload_limit"]["status"], "at_least")
        self.assertEqual(report["upload_limit"]["largest_unblocked_bytes"], 1_000_000)
        self.assertIsNone(report["upload_limit"]["smallest_blocked_bytes"])
        self.assertEqual(report["upload_limit"]["unblocked_samples"], 50)
        self.assertEqual([call["bytes"] for call in upstream.calls], list(range(20_000, 1_000_001, 20_000)))

    def test_scan_records_generic_403_without_treating_it_as_a_size_limit(self):
        upstream, relay = self.gateway(), self.gateway()
        report_dir = self.root / "unknown-report"
        result = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.ps1),
                                 "-Target", f"http://127.0.0.1:{upstream.server_port}/forbidden/responses",
                                 "-RelayPort", str(relay.server_port), "-OutputDirectory", str(report_dir),
                                 "-FindUploadLimit"],
                                env=self.environment, capture_output=True, text=True, encoding="utf8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads((report_dir / "report.json").read_text(encoding="utf8"))
        self.assertEqual(report["upload_limit"]["status"], "inconclusive")
        self.assertIsNone(report["upload_limit"]["largest_unblocked_bytes"])
        self.assertIsNone(report["upload_limit"]["smallest_blocked_bytes"])
        self.assertEqual(report["upload_limit"]["unknown_samples"], 50)
        self.assertEqual(len(upstream.calls), 50)

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
        self.assertEqual([c["status"] for c in report["conclusions"]][-2:], ["unknown", "unknown"])
        self.assertEqual(len(upstream.calls), 1, "No automatic retry of a failed test")


if __name__ == "__main__":
    unittest.main()
