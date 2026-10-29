@echo off
setlocal
chcp 65001 >nul
set "CODEX_NETWORK_TEST_SCRIPT=%~f0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$raw=Get-Content -LiteralPath $env:CODEX_NETWORK_TEST_SCRIPT -Raw -Encoding UTF8; $marker=':__POWERSHELL__'; & ([scriptblock]::Create($raw.Substring($raw.LastIndexOf($marker)+$marker.Length)))"
set "probe_exit=%errorlevel%"
echo.
if not "%CODEX_NETWORK_TEST_NO_PAUSE%"=="1" pause
exit /b %probe_exit%
:__POWERSHELL__
param(
    [string]$Target = '',
    [int]$RelayPort = 47844,
    [string]$OutputDirectory = '',
    [int]$TimeoutSeconds = 20,
    [switch]$LoadOnly
)

# Self-contained Windows PowerShell 5.1 diagnostic. The CMD prefix launches
# this payload in one process; no Python, installation or admin rights needed.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$script:Utf8 = New-Object System.Text.UTF8Encoding($false)
$script:ResponseLimit = 65536

function Protect-Text([object]$Value) {
    $text = [string]$Value
    $text = [regex]::Replace($text, '(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/-]+=*', '$1 [redacted]')
    $text = [regex]::Replace($text, '(?i)(https?://)[^/\s<>"'']*@', '$1[redacted]@')
    $text = [regex]::Replace($text, '(?i)(^|[;\s=])[^;\s/@:]+:[^;\s/@]+@', '$1[redacted]@')
    $text = [regex]::Replace($text, '(?i)(https?://[^\s<>"''?#]+)\?[^\s<>"'']*', '$1?[redacted]')
    $text = [regex]::Replace($text, '(?im)\b(authorization|proxy-authorization|cookie|set-cookie)\s*[:=][^\r\n]*', '$1: [redacted]')
    $text = [regex]::Replace($text, '(?i)(["'']?(?:password|access_token|refresh_token|api_key|client_secret)["'']?\s*[:=]\s*)["'']?[^\s,;<>]+', '$1[redacted]')
    return $text
}

function Write-Utf8([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText($Path, $Text, $script:Utf8)
}

function Get-ProxyContext {
    $registry = Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue
    $environment = @{}
    foreach ($item in Get-ChildItem Env:) {
        if ($item.Name -match '^(\w+)_proxy$' -and $item.Value) { $environment[$Matches[1].ToLowerInvariant()] = $item.Value }
    }
    $map = @{}
    $environmentProxy = @($environment.Keys | Where-Object { $_ -ne 'no' }).Count -gt 0
    $source = 'direct'
    if ($environmentProxy) {
        foreach ($key in $environment.Keys) { if ($key -ne 'no') { $map[$key] = $environment[$key] } }
        $source = 'environment'
    } elseif ($registry -and $registry.ProxyEnable -and $registry.ProxyServer) {
        $server = [string]$registry.ProxyServer
        if ($server.Contains('=')) {
            foreach ($part in $server.Split(';')) {
                if ($part -match '^\s*(\w+)=(.+?)\s*$') { $map[$Matches[1].ToLowerInvariant()] = $Matches[2] }
            }
        } else { $map['http'] = $server; $map['https'] = $server }
        $source = 'windows_manual'
    }
    $safeEnvironment = @{}
    foreach ($key in $environment.Keys) {
        $value = [string]$environment[$key]
        if ($key -ne 'no' -and $value -notmatch '^\w+://') { $value = 'http://' + $value }
        $safeEnvironment[$key] = Protect-Text $value
    }
    return @{
        map = $map; source = $source; environment = $safeEnvironment
        bypass_mode = $(if ($environment.Count) { 'environment' } else { 'windows' })
        no_proxy = [string]$environment['no']
        windows_enabled = [bool]($registry -and $registry.ProxyEnable)
        windows_server = Protect-Text $(if ($registry) { $registry.ProxyServer } else { '' })
        windows_bypass = $(if ($registry) { [string]$registry.ProxyOverride } else { '' })
        pac_configured = [bool]($registry -and $registry.AutoConfigURL)
        auto_detect = $(if ($registry -and $null -ne $registry.AutoDetect) { [bool]$registry.AutoDetect } else { $null })
    }
}

function Get-Route([uri]$Uri, [hashtable]$Context, [switch]$Direct) {
    $bypass = [bool]$Direct
    $hostName = $Uri.Host.Trim('[', ']').ToLowerInvariant()
    $parsedIP = $null
    if ([Net.IPAddress]::TryParse($hostName, [ref]$parsedIP)) { $hostName = $parsedIP.ToString() }
    if (-not $bypass -and $Context.bypass_mode -eq 'environment') {
        foreach ($part in $Context.no_proxy.Split(',')) {
            $entry = $part.Trim().ToLowerInvariant()
            if ($entry -eq '*') { $bypass = $true; break }
            if (-not $entry) { continue }
            if ($entry -match '^\[([^\]]+)\](?::(\d+))?$') {
                if ($hostName -eq $Matches[1] -and (-not $Matches[2] -or $Uri.Port -eq [int]$Matches[2])) { $bypass = $true; break }
                continue
            }
            $entry = $entry.TrimStart('.')
            $hostPort = $hostName + ':' + $Uri.Port
            if ($hostName -eq $entry -or $hostName.EndsWith('.' + $entry) -or $hostPort -eq $entry -or $hostPort.EndsWith('.' + $entry)) {
                $bypass = $true; break
            }
        }
    } elseif (-not $bypass) {
        foreach ($part in $Context.windows_bypass.Split(';')) {
            $entry = $part.Trim()
            if (-not $entry) { continue }
            if ($entry -eq '<local>' -and -not $hostName.Contains('.') -and -not $hostName.Contains(':')) { $bypass = $true; break }
            $pattern = '^' + [regex]::Escape($entry).Replace('\*', '.*').Replace('\?', '.') + '$'
            if ($hostName -match $pattern -or ($hostName + ':' + $Uri.Port) -match $pattern) { $bypass = $true; break }
        }
    }
    $proxy = $null
    $safeProxy = ''
    if (-not $bypass -and $Context.map.ContainsKey($Uri.Scheme)) {
        $address = [string]$Context.map[$Uri.Scheme]
        if ($address -notmatch '^\w+://') { $address = 'http://' + $address }
        $proxyUri = [uri]$address
        if ($proxyUri.Scheme -notin @('http', 'https') -or -not $proxyUri.Host) { throw 'Unsupported proxy URI' }
        # Do not transmit credentials embedded in environment proxy URLs.
        $builder = New-Object UriBuilder($proxyUri)
        $builder.UserName = ''; $builder.Password = ''; $builder.Query = ''; $builder.Fragment = ''
        $proxy = $builder.Uri.AbsoluteUri
        $safeProxy = $proxy
    }
    return @{ proxy = $proxy; mode = $(if ($proxy) { 'proxy' } else { 'direct' }); endpoint = $safeProxy; bypass = $bypass; source = $Context.source }
}

function Get-ExceptionDetails([Exception]$Exception) {
    $items = @()
    for ($current = $Exception; $current -and $items.Count -lt 5; $current = $current.InnerException) {
        $item = @{ type = $current.GetType().FullName; message = Protect-Text $current.Message }
        if ($current -is [Net.WebException]) { $item['web_status'] = [string]$current.Status }
        if ($current -is [Net.Sockets.SocketException]) { $item['socket_error'] = [string]$current.SocketErrorCode; $item['native_error'] = $current.NativeErrorCode }
        $items += $item
    }
    return $items
}

function Quote-NativeArgument([string]$Value) {
    # Windows command-line quoting; Process.Start does not invoke a shell.
    $value = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $value = [regex]::Replace($value, '(\\+)$', '$1$1')
    return '"' + $value + '"'
}

function Read-LimitedText([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    $file = [IO.File]::OpenRead($Path)
    try {
        $buffer = New-Object byte[] ($script:ResponseLimit + 1)
        $length = $file.Read($buffer, 0, $buffer.Length)
        return Protect-Text ([Text.Encoding]::UTF8.GetString($buffer, 0, [Math]::Min($length, $script:ResponseLimit)))
    } finally { $file.Dispose() }
}

function Invoke-Probe([string]$Key, [uri]$Uri, [hashtable]$Route, [int]$Bytes, [int]$Seconds) {
    $result = @{ key = $Key; url = Protect-Text $Uri.AbsoluteUri; bytes = $Bytes; written_bytes = 0
        route = @{ mode = $Route.mode; source = $Route.source; proxy = $Route.endpoint; bypass = $Route.bypass }
        authentication_sent = $false; encoding = 'identity'; headers = @{}; body = ''; status = 0 }
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $process = $null; $files = @()
    try {
        $curl = (Get-Command curl.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
        $prefix = Join-Path ([IO.Path]::GetTempPath()) ('codex-network-test-' + [guid]::NewGuid().ToString('N'))
        $headerPath = $prefix + '.headers'; $bodyPath = $prefix + '.response'; $tracePath = $prefix + '.trace'; $requestPath = $prefix + '.request'
        $files = @($headerPath, $bodyPath, $tracePath, $requestPath)
        $format = 'http_code=%{http_code}\nhttp_connect=%{http_connect}\nremote_ip=%{remote_ip}\nremote_port=%{remote_port}\ntime_namelookup=%{time_namelookup}\ntime_connect=%{time_connect}\ntime_appconnect=%{time_appconnect}\ntime_starttransfer=%{time_starttransfer}\ntime_total=%{time_total}\nsize_upload=%{size_upload}\nsize_download=%{size_download}\nssl_verify_result=%{ssl_verify_result}\n'
        # --disable must be first: an existing curlrc must not inject auth,
        # retries, a different proxy or other per-user request behavior.
        $arguments = @('--disable', '--silent', '--show-error', '--verbose', '--http1.1', '--retry', '0', '--connect-timeout', '5',
            '--max-time', [string]$Seconds, '--max-filesize', [string]$script:ResponseLimit,
            '--noproxy', '', '--proxy', [string]$Route.endpoint,
            '--user-agent', 'CodexRequestOptimizer/StandaloneNetworkTest', '--header', 'Accept: application/json',
            '--header', 'Accept-Encoding: identity', '--header', 'Connection: close', '--header', 'Expect:',
            '--dump-header', $headerPath, '--output', $bodyPath, '--stderr', $tracePath, '--write-out', $format)
        if ($Bytes -gt 0) {
            # Malformed JSON cannot start inference, even if an upstream
            # injects authentication or supplies a default model.
            $padding = '{"connection_test":"synthetic-no-inference","padding":"'
            $body = [Text.Encoding]::ASCII.GetBytes($padding + ('0' * ($Bytes - $padding.Length)))
            [IO.File]::WriteAllBytes($requestPath, $body)
            $arguments += @('--header', 'Content-Type: application/json', '--header', 'Content-Encoding: identity',
                '--data-binary', ('@' + $requestPath))
        }
        $arguments += @('--url', $Uri.AbsoluteUri)
        $process = New-Object Diagnostics.Process
        $process.StartInfo.FileName = $curl
        $process.StartInfo.Arguments = (($arguments | ForEach-Object { Quote-NativeArgument $_ }) -join ' ')
        $process.StartInfo.UseShellExecute = $false; $process.StartInfo.CreateNoWindow = $true
        $process.StartInfo.RedirectStandardOutput = $true; $process.StartInfo.RedirectStandardError = $true
        [void]$process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync(); $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(($Seconds + 3) * 1000)) {
            $process.Kill(); $process.WaitForExit(); throw 'curl exceeded its deadline'
        }
        $result['curl_exit'] = $process.ExitCode
        $metrics = @{}
        foreach ($line in ($stdout.Result -split '\r?\n')) {
            $pair = $line -split '=', 2
            if ($pair.Count -eq 2) { $metrics[$pair[0]] = $pair[1] }
        }
        $result['flow'] = $metrics
        if ($metrics['http_code']) { $result.status = [int]$metrics['http_code'] }
        if (-not $result.status -and [int]$metrics['http_connect'] -ge 400) { $result.status = [int]$metrics['http_connect'] }
        if ($metrics['size_upload']) { $result.written_bytes = [int64][double]::Parse($metrics['size_upload'], [Globalization.CultureInfo]::InvariantCulture) }
        $headers = Read-LimitedText $headerPath
        foreach ($line in ($headers -split '\r?\n')) {
            # Discard the CONNECT/interim header set when the final one starts.
            if ($line -match '^HTTP/\S+ \d{3}') { $result.headers = @{}; continue }
            $pair = $line -split ':', 2
            if ($pair.Count -eq 2 -and $pair[0] -match '^(Content-(Type|Length|Encoding)|Server|Date|Via|Proxy-Agent|X-(Request-Id|Squid-Error|Correlation-Id)|CF-Ray|Location)$') {
                $result.headers[$pair[0].ToLowerInvariant()] = $pair[1].Trim()
            } elseif ($pair.Count -eq 2 -and $pair[0] -match '^(WWW|Proxy)-Authenticate$') {
                $result.headers[$pair[0].ToLowerInvariant()] = ($pair[1].Trim() -split '\s+')[0]
            }
        }
        $result.body = Read-LimitedText $bodyPath
        $result['response_truncated'] = $process.ExitCode -eq 63 -or ((Test-Path -LiteralPath $bodyPath) -and (Get-Item -LiteralPath $bodyPath).Length -gt $script:ResponseLimit)
        $result['transport_detail'] = Protect-Text ((Read-LimitedText $tracePath) + $stderr.Result)
        if ($process.ExitCode -ne 0) {
            $result['exceptions'] = @(@{ type = 'curl'; message = ('curl exit ' + $process.ExitCode) })
        }
    } catch { $result['exceptions'] = @(Get-ExceptionDetails $_.Exception) }
    finally {
        if ($process) { $process.Dispose() }
        # Only these four exact temporary files belong to this invocation.
        foreach ($path in $files) { Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue }
        $result['elapsed_ms'] = $timer.ElapsedMilliseconds
    }
    $verdict = Get-ProbeVerdict $result ($Bytes -eq 0)
    $result['verdict'] = $verdict.code; $result['message'] = $verdict.message
    return $result
}


function Get-ProbeVerdict([hashtable]$Result, [bool]$Local) {
    $body = [string]$Result.body
    $gateway = $Result.headers['server'] -match 'netentsec' -or $body -match 'HIS Proxy Notification'
    if ($Result.status -eq 407) { return @{ code = 'proxy_auth'; message = '代理要求认证（407）' } }
    if ($gateway -and $body -match '该网站上传文件受限|申请上传|upload.{0,80}(restrict|block|permission)') { return @{ code = 'upload_blocked'; message = '企业网关限制上传' } }
    if ($Result.status -eq 413) { return @{ code = 'size_blocked'; message = '请求大小被限制（413）' } }
    if ($gateway) { return @{ code = 'gateway_error'; message = '企业网关报错' } }
    if ($Result.exceptions) { return @{ code = 'network_error'; message = '连接或读取失败' } }
    $json = $null
    try { $json = $body | ConvertFrom-Json -ErrorAction Stop } catch { }
    if ($Local) {
        if ($Result.status -eq 200 -and $json.app -eq 'codex-packet-inspector' -and $json.ok -eq $true) { return @{ code = 'local_ok'; message = '本机入口正常' } }
        return @{ code = 'local_failed'; message = '未收到本机中转的健康响应' }
    }
    if ($Result.status -in @(400, 401, 404, 422) -and $Result.headers['content-type'] -match 'json' -and ($json.error -or $json.detail) -and $Result.written_bytes -eq $Result.bytes) {
        return @{ code = 'api_response'; message = '收到 API 拒绝响应，本次未见上传拦截' }
    }
    return @{ code = 'unknown'; message = '无法确认上传权限，查看响应详情' }
}

function Get-AddressProbe([string]$Name, [int]$Port) {
    $result = @{ host = $Name; port = $Port }
    try {
        $dns = [Net.Dns]::GetHostAddressesAsync($Name)
        if (-not $dns.Wait(3000)) { throw 'DNS timeout (3s)' }
        $result['addresses'] = @($dns.Result | ForEach-Object { $_.IPAddressToString })
        $tcp = New-Object Net.Sockets.TcpClient
        try {
            $connect = $tcp.BeginConnect($Name, $Port, $null, $null)
            if (-not $connect.AsyncWaitHandle.WaitOne(3000)) { throw 'TCP connect timeout (3s)' }
            $tcp.EndConnect($connect); $result['tcp'] = 'connected'
            $result['peer'] = [string]$tcp.Client.RemoteEndPoint
            $connect.AsyncWaitHandle.Close()
        } finally { $tcp.Dispose() }
    } catch { $result['exceptions'] = @(Get-ExceptionDetails $_.Exception) }
    return $result
}

function Start-NetworkTest {
    if ($RelayPort -lt 1 -or $RelayPort -gt 65535 -or $TimeoutSeconds -lt 1 -or $TimeoutSeconds -gt 60) { throw 'Invalid port or timeout' }
    $scriptPath = $env:CODEX_NETWORK_TEST_SCRIPT
    if (-not $OutputDirectory) {
        $parent = $(if ($scriptPath) { [IO.Path]::GetDirectoryName($scriptPath) } else { $PSScriptRoot })
        $OutputDirectory = Join-Path $parent ('CodexNetworkTest-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 6))
    }
    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
    $OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
    $script:ReportDirectory = $OutputDirectory
    $curlPath = (Get-Command curl.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $curlVersion = (& $curlPath --disable --version | Select-Object -First 1)
    Write-Host 'Codex 代理与上传测试' -ForegroundColor Cyan
    Write-Host '不修改配置；不发送 Codex 登录凭据；不调用模型。1 MB = 1,000,000 字节。'
    Write-Host ('结果目录：' + $OutputDirectory)
    $context = Get-ProxyContext
    $local = [uri]('http://127.0.0.1:' + $RelayPort + '/health')
    $direct = Get-Route $local $context -Direct
    $results = @()
    Write-Host '[1/5] 检查本机入口（直连）…'
    $results += Invoke-Probe 'local_direct' $local $direct 0 3
    if (-not $Target -and $results[0].verdict -eq 'local_ok') {
        $settings = Invoke-Probe 'saved_upstream' ([uri]('http://127.0.0.1:' + $RelayPort + '/api/status')) $direct 0 3
        try {
            $status = $settings.body | ConvertFrom-Json -ErrorAction Stop
            $base = $(if ($status.profile.upstream) { [string]$status.profile.upstream } else { [string]$status.upstream })
            if ($base) { $Target = $base.TrimEnd('/') + '/responses' }
        } catch { }
    }
    if (-not $Target) { $Target = 'https://chatgpt.com/backend-api/codex/responses' }
    $destination = [uri]$Target
    $loopback = $destination.Host -in @('localhost', '127.0.0.1', '[::1]', '::1')
    if ($destination.Scheme -notin @('http', 'https') -or (-not $loopback -and $destination.Scheme -ne 'https') -or $destination.UserInfo -or $destination.Query -or $destination.Fragment) { throw 'Invalid target URL; remote targets require HTTPS without credentials or query parameters' }
    if ($loopback -and $destination.Port -eq $RelayPort) { throw 'Refusing to upload test bodies to the relay itself' }
    $route = Get-Route $destination $context -Direct:$loopback
    $safeContext = @{}
    foreach ($key in $context.Keys) { if ($key -ne 'map') { $safeContext[$key] = $context[$key] } }
    $report = @{ version = 1; started = (Get-Date).ToUniversalTime().ToString('o'); hostname = [Environment]::MachineName
        os = [Environment]::OSVersion.VersionString; powershell = [string]$PSVersionTable.PSVersion
        target = $destination.AbsoluteUri; proxy = $safeContext; results = @(); authentication_sent = $false; curl_version = [string]$curlVersion
        note = 'Synthetic malformed JSON, identity encoding. No inference. API rejection is sample evidence, not formal permission approval. Native ChatGPT uses a different network stack.' }
    Write-Host ('上游：' + $report.target)
    Write-Host ('代理来源：' + $route.source + '；本次路径：' + $(if ($route.endpoint) { $route.endpoint } else { 'direct' }))
    if ($context.pac_configured) { Write-Host '检测到 PAC；本测试按中转的环境变量/手动代理规则选路，不执行 PAC。' -ForegroundColor Yellow }
    Write-Host '[2/5] 检查本机入口（当前代理规则）…'
    $results += Invoke-Probe 'local_proxy' $local (Get-Route $local $context) 0 5
    $peer = $(if ($route.endpoint) { [uri]$route.endpoint } else { $destination })
    $report['address_probe'] = Get-AddressProbe $peer.DnsSafeHost $peer.Port
    $sizes = @(64000, 950000, 1100000)
    $stopUploads = $false
    for ($index = 0; $index -lt $sizes.Count; $index++) {
        $size = $sizes[$index]
        Write-Host ('[' + ($index + 3) + '/5] 测试上传 ' + ($size / 1000) + ' kB…')
        if ($stopUploads) {
            $results += @{ key = 'upload_' + $size; bytes = $size; status = 0; verdict = 'skipped'; message = '上一项连接失败，已跳过'; body = ''; headers = @{} }
        } else {
            $probe = Invoke-Probe ('upload_' + $size) $destination $route $size $TimeoutSeconds
            $results += $probe
            Write-Host ('  HTTP ' + $probe.status + ' · ' + $probe.message)
            $stopUploads = $probe.verdict -in @('network_error', 'proxy_auth', 'gateway_error')
        }
        $report.results = $results; Write-Utf8 (Join-Path $OutputDirectory 'report.json') ($report | ConvertTo-Json -Depth 14)
    }
    $lines = @('Codex 代理与上传测试', ('时间：' + $report.started), ('电脑：' + $report.hostname), ('上游：' + $report.target), ('代理来源：' + $route.source), ('本次代理：' + $route.endpoint), '')
    foreach ($result in $results) {
        $http = $(if ($result.status) { 'HTTP ' + $result.status } else { '无 HTTP 响应' })
        $lines += $result.key + ' | ' + $http + ' | ' + $result.message
        $details = @($http)
        foreach ($key in $result.headers.Keys) { $details += $key + ': ' + $result.headers[$key] }
        $details += ''; $details += $result.body
        if ($result.transport_detail) { $details += ''; $details += $result.transport_detail }
        if ($result.exceptions) { $details += ($result.exceptions | ConvertTo-Json -Depth 8) }
        Write-Utf8 (Join-Path $OutputDirectory ($result.key + '-response.txt')) ($details -join "`r`n")
    }
    if ($results[0].verdict -eq 'local_ok' -and $results[1].verdict -ne 'local_ok') {
        $lines += ''; $lines += '本机直连正常，按当前代理规则访问失败。请核对 127.0.0.1、localhost、[::1] 的代理例外。'
    }
    $uploads = @($results | Where-Object { $_.bytes -gt 0 })
    if (@($uploads | Where-Object { $_.verdict -eq 'api_response' }).Count -eq 3) {
        $lines += ''; $lines += '三种大小均收到 API 响应，本次未见上传拦截。'
    } elseif ($uploads[0].verdict -eq 'upload_blocked') {
        $lines += ''; $lines += '64 kB 小包已被企业网关限制；请核对小于 1 MB 的上传权限。'
    } elseif ($uploads[1].verdict -eq 'api_response' -and $uploads[2].verdict -eq 'upload_blocked') {
        $lines += ''; $lines += '950 kB 样本未见拦截，1,100 kB 样本被网关限制；请核对大于 1 MB 的上传权限。'
    } else { $lines += ''; $lines += '无法完整判断 1 MB 上传权限，请结合各档响应正文排查。' }
    $lines += '400/401 等 API 拒绝响应只说明本次合成样本的路径结果，不能替代正式上传权限审批。'
    $lines += '本机探测使用本脚本的网络栈；ChatGPT 实际接入以中转收到请求为准。'
    $report['finished'] = (Get-Date).ToUniversalTime().ToString('o'); $report.results = $results
    Write-Utf8 (Join-Path $OutputDirectory 'report.json') ($report | ConvertTo-Json -Depth 14)
    Write-Utf8 (Join-Path $OutputDirectory 'summary.txt') ($lines -join "`r`n")
    Write-Host ''; foreach ($line in $lines) { Write-Host $line }
    Write-Host ('请将此目录完整发回排查：' + $OutputDirectory) -ForegroundColor Cyan
    return $report
}

if (-not $LoadOnly) {
    try { $null = Start-NetworkTest }
    catch {
        Write-Host ('测试未完成：' + (Protect-Text $_.Exception.Message)) -ForegroundColor Red
        if ($script:ReportDirectory -and (Test-Path -LiteralPath $script:ReportDirectory)) {
            Write-Utf8 (Join-Path $script:ReportDirectory 'fatal-error.txt') ((Get-ExceptionDetails $_.Exception) | ConvertTo-Json -Depth 8)
            Write-Host ('错误详情：' + $script:ReportDirectory)
        }
        exit 1
    }
}
