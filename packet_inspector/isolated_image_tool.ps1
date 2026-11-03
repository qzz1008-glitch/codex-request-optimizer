param([switch]$LibraryMode)

# Independent MCP stdio program. It has no dependency on the inspector process.
$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$script:ImageCalls = 0
$script:Initialized = $false
. (Join-Path $PSScriptRoot 'image_session.ps1')

function Find-CodexCli {
    if ($env:CODEX_CLI_PATH -and (Test-Path -LiteralPath $env:CODEX_CLI_PATH -PathType Leaf)) {
        return $env:CODEX_CLI_PATH
    }
    $found = Get-Command codex.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { return $found.Source }
    if ($env:LOCALAPPDATA) {
        $directory = Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin'
        $found = Get-ChildItem -LiteralPath $directory -Filter codex.exe -Recurse -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    throw 'Codex CLI is unavailable. Open or install Codex and retry.'
}

function Quote-ProcessArgument([string]$Value) {
    # Windows CreateProcess quoting; no shell or command interpolation is used.
    return '"' + [regex]::Replace([regex]::Replace($Value, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1') + '"'
}

function Invoke-ToolProcess([string]$Executable, [string[]]$Arguments, [int]$TimeoutSeconds, [string]$WorkingDirectory, [string]$InputText = $null) {
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $Executable
    $start.Arguments = (($Arguments | ForEach-Object { Quote-ProcessArgument $_ }) -join ' ')
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.StandardOutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $start.StandardErrorEncoding = New-Object System.Text.UTF8Encoding($false)
    $start.WorkingDirectory = $WorkingDirectory
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    try {
        if (-not $process.Start()) { throw 'Unable to start image analysis process.' }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if ($InputText) { $process.StandardInput.Write($InputText) }
        $process.StandardInput.Close()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $process.Kill()
            $process.WaitForExit()
            throw 'Independent image analysis timed out.'
        }
        $output = $stdout.GetAwaiter().GetResult()
        $errorOutput = $stderr.GetAwaiter().GetResult()
        return @{ ExitCode = $process.ExitCode; Output = $output; Error = $errorOutput }
    } finally { $process.Dispose() }
}

function Get-ToolFailureDetail($Result, [int]$MaxChars = 1000) {
    $detail = [string]$Result.Error
    $detail = [regex]::Replace($detail, '(?i)data:image/[^\s;,]+;base64,[A-Za-z0-9+/=\r\n]+', '[image data omitted]')
    $detail = [regex]::Replace($detail, '[A-Za-z0-9+/]{512,}={0,2}', '[opaque data omitted]')
    $detail = [regex]::Replace($detail, '(?i)\b(Bearer|Basic)\s+[^\s,"'']+', '$1 [redacted]')
    $detail = [regex]::Replace($detail, '(?i)\bsk-[A-Za-z0-9_-]+', '[redacted]')
    $detail = [regex]::Replace($detail, '(?i)(/relay/)[^/\s]+', '$1[redacted]')
    $detail = [regex]::Replace($detail, '(?i)([?&](?:api_key|access_token|refresh_token|token|key|signature)=)[^&\s]+', '$1[redacted]')
    $detail = $detail.Trim()
    if ($detail.Length -gt $MaxChars) { $detail = '[stderr truncated] ' + $detail.Substring($detail.Length - $MaxChars) }
    if ($detail) { return ("`n" + $detail) }
    return ''
}

function Get-ImageParentThreadId($Metadata) {
    # MCP servers may serve multiple threads: read the current call, never process-wide environment.
    $value = $Metadata.threadId
    if ($value -is [string] -and $value -match '\A[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\z') {
        return $value.ToLowerInvariant()
    }
    return $null
}

function Invoke-ImageAnalysis($Arguments, [string]$ParentThreadId) {
    if (-not $Arguments -or $Arguments -is [string] -or $Arguments -is [array]) { throw 'Invalid image tool arguments.' }
    foreach ($name in $Arguments.PSObject.Properties.Name) {
        if ($name -notin @('path', 'instruction', 'max_output_chars')) { throw 'Invalid image tool arguments.' }
    }
    $path = $Arguments.path
    if ($path -isnot [string] -or $path.Contains([char]0) -or $path -notmatch '^(?:[A-Za-z]:[\\/]|\\\\)') {
        throw 'path must be an absolute local image path.'
    }
    $image = Get-Item -LiteralPath $path -Force -ErrorAction Stop
    if ($image.PSIsContainer -or $image.Extension.ToLowerInvariant() -notin @('.png', '.jpg', '.jpeg', '.webp', '.gif')) {
        throw 'path must identify a PNG, JPEG, WebP or GIF file.'
    }
    if ($image.Length -le 0 -or $image.Length -gt 52428800) { throw 'Image is empty or exceeds 52.4288 MB.' }
    $instruction = 'Describe this image, including its visible text and any uncertainty.'
    if ($Arguments.PSObject.Properties['instruction']) { $instruction = $Arguments.instruction }
    if ($instruction -isnot [string] -or -not $instruction.Trim() -or $instruction.Length -gt 4000) {
        throw 'instruction must contain 1 to 4000 characters.'
    }
    $limit = 8000
    if ($Arguments.PSObject.Properties['max_output_chars']) { $limit = $Arguments.max_output_chars }
    if (($limit -isnot [int] -and $limit -isnot [long]) -or $limit -lt 200 -or $limit -gt 20000) {
        throw 'max_output_chars must be between 200 and 20000.'
    }
    $cli = Find-CodexCli
    # Leave time for cleanup within the MCP registration's 420-second timeout,
    # including an optional rollover; the second turn does not reset this clock.
    $script:ImageCallDeadline = [datetime]::UtcNow.AddSeconds(380)
    $hostState = Get-ImageHost $cli
    $digest = Get-ImageDigest $image.FullName
    # Missing per-call parent identity must never share image conversations.
    $scope = if ($ParentThreadId) { $ParentThreadId } else { [guid]::NewGuid().ToString('N') }
    $key = $scope + ':' + $digest
    $session = $script:ImageSessions[$key]
    if (-not $session) {
        $snapshot = Join-Path $hostState.Work ($digest + $image.Extension.ToLowerInvariant())
        if (-not (Test-Path -LiteralPath $snapshot -PathType Leaf)) {
            Copy-Item -LiteralPath $image.FullName -Destination $snapshot
            if ((Get-ImageDigest $snapshot) -ne $digest) {
                Remove-Item -LiteralPath $snapshot -Force
                throw 'Image changed while preparing analysis; retry with the current file.'
            }
        }
        $upload = $snapshot
        if ($image.Length -gt 409600) {
            $upload = Join-Path $hostState.Work ($digest + '-upload.jpg')
            if (-not (Test-Path -LiteralPath $upload -PathType Leaf)) {
                $helper = Join-Path $PSScriptRoot 'compress_image.ps1'
                $powershell = Join-Path $PSHOME 'powershell.exe'
                if (-not (Test-Path -LiteralPath $powershell -PathType Leaf)) { $powershell = (Get-Process -Id $PID).Path }
                $converted = Invoke-ToolProcess $powershell @('-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
                    '-File',$helper,'-InputPath',$snapshot,'-OutputPath',$upload,'-MaxBytes','409600','-MaxDimension','1600') 90 $hostState.Work
                if ($converted.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $upload -PathType Leaf) -or
                    (Get-Item -LiteralPath $upload).Length -gt 409600) { throw 'Image compression failed.' }
            }
            Remove-Item -LiteralPath $snapshot -Force
        }
        $session = New-ImageThread $hostState $upload $image.FullName $ParentThreadId
        $script:ImageSessions[$key] = $session
    }
    try {
        try { $answer = Invoke-ImageTurn $hostState $session $instruction }
        catch {
            if (-not $_.Exception.Data['ImageSessionLimit'] -or $session.Turns -eq 0) { throw }
            Forget-ImageThread $hostState $session
            $session = New-ImageThread $hostState $session.Upload $image.FullName $ParentThreadId
            $script:ImageSessions[$key] = $session
            $answer = Invoke-ImageTurn $hostState $session $instruction
        }
        $answer = [regex]::Replace($answer, '(?i)data:image/[^\s;,]+;base64,[A-Za-z0-9+/=\r\n]+', '[image data omitted]')
        $answer = [regex]::Replace($answer, '[A-Za-z0-9+/]{512,}={0,2}', '[image data omitted]')
        if ($answer.Length -gt $limit) { $answer = $answer.Substring(0, $limit) + "`n[Result truncated]" }
        return $answer
    } finally {
        if (-not $ParentThreadId) {
            $script:ImageSessions.Remove($key)
            Forget-ImageThread $hostState $session
        }
    }
}

function Invoke-McpRequest($Message) {
    if (-not $Message -or $Message.jsonrpc -ne '2.0' -or $Message.method -isnot [string]) {
        return @{jsonrpc='2.0'; id=$null; error=@{code=-32600; message='Invalid MCP request.'}}
    }
    $hasId = $null -ne $Message.PSObject.Properties['id']
    if ($Message.method.StartsWith('notifications/')) { return $null }
    if (-not $hasId) { return $null }
    $reply = @{jsonrpc='2.0'; id=$Message.id}
    if ($Message.method -eq 'initialize') {
        $script:Initialized = $true
        $version = $Message.params.protocolVersion
        if ($version -notin @('2025-03-26','2025-06-18','2025-11-25')) { $version = '2025-06-18' }
        $reply.result = @{protocolVersion=$version; capabilities=@{tools=@{}}; serverInfo=@{name='codex-isolated-vision';version='2.1.0'}}
    } elseif (-not $script:Initialized) {
        $reply.error = @{code=-32000;message='Initialize the image tool first.'}
    } elseif ($Message.method -eq 'ping') {
        $reply.result = @{}
    } elseif ($Message.method -eq 'tools/list') {
        $reply.result = @{tools=@(@{name='analyze_image';description=(
            'Analyze one user-requested local image in an independent native Codex conversation. ' +
            'Follow-up questions about the same unchanged image in this parent session reuse its image conversation. Only text returns. ' +
            'Do not use for video frames, directory scans or batch analysis. At most 24 calls per session.');
            inputSchema=@{type='object';properties=@{path=@{type='string';description='Absolute path of one local PNG, JPEG, WebP or GIF.'};
                instruction=@{type='string';maxLength=4000};max_output_chars=@{type='integer';minimum=200;maximum=20000;default=8000}};
                required=@('path');additionalProperties=$false};
            annotations=@{readOnlyHint=$true;destructiveHint=$false;openWorldHint=$true}})}
    } elseif ($Message.method -eq 'tools/call') {
        try {
            if ($Message.params.name -ne 'analyze_image') { throw 'Unknown image tool.' }
            if ($script:ImageCalls -ge 24) { throw 'Image analysis call limit reached (24); use existing text results.' }
            $script:ImageCalls++
            $parentThreadId = Get-ImageParentThreadId $Message.params._meta
            $answer = Invoke-ImageAnalysis $Message.params.arguments $parentThreadId
            $reply.result = @{content=@(@{type='text';text=$answer})}
        } catch {
            $errorText = $_.Exception.Message
            if ($_.Exception.Data['ImageHostUnusable'] -or
                ($script:VisionHost -and $script:VisionHost.Process.HasExited)) { Close-ImageSessions }
            $errorText = (Get-ToolFailureDetail @{Error=$errorText} 1400).Trim()
            if ($errorText.Length -gt 1400) { $errorText = $errorText.Substring(0,1400) }
            $reply.result = @{content=@(@{type='text';text=$errorText});isError=$true}
        } finally { if ($script:ImageCalls -ge 24) { Close-ImageSessions } }
    } else { $reply.error = @{code=-32601;message='Method not found.'} }
    return $reply
}

if (-not $LibraryMode) {
    try {
        while ($null -ne ($line = [Console]::In.ReadLine())) {
            if (-not $line.Trim()) { continue }
            try {
                if ($line.Length -gt 131072) { throw 'MCP request is too large.' }
                $message = $line | ConvertFrom-Json
                $reply = Invoke-McpRequest $message
            } catch { $reply = @{jsonrpc='2.0';id=$null;error=@{code=-32700;message='Invalid MCP JSON.'}} }
            if ($null -ne $reply) {
                [Console]::Out.WriteLine(($reply | ConvertTo-Json -Depth 40 -Compress))
                [Console]::Out.Flush()
            }
        }
    } finally { Close-ImageSessions }
}
