@echo off
setlocal
chcp 65001 >nul
set "CODEX_ROUTING_REPORT_SCRIPT=%~f0"
set "CODEX_ROUTING_REPORT_THREAD=%~1"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$raw=Get-Content -LiteralPath $env:CODEX_ROUTING_REPORT_SCRIPT -Raw -Encoding UTF8; $marker=':__POWERSHELL__'; & ([scriptblock]::Create($raw.Substring($raw.LastIndexOf($marker)+$marker.Length))) -ThreadId $env:CODEX_ROUTING_REPORT_THREAD"
set "report_exit=%errorlevel%"
echo.
if not "%CODEX_ROUTING_REPORT_NO_PAUSE%"=="1" pause
exit /b %report_exit%
:__POWERSHELL__
param([string]$ThreadId='', [string]$OutputDirectory='', [int]$RelayPort=47844, [switch]$LoadOnly)

# Standalone Windows PowerShell 5.1 collector. No inference or write RPCs.
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)
$OutputEncoding=[Console]::OutputEncoding
$script:Utf8=New-Object System.Text.UTF8Encoding($false)

function Protect-RoutingText([object]$Value) {
    $text=[string]$Value
    $text=[regex]::Replace($text,'(?i)(/relay/)[^/\s"''<>\\]+','$1[redacted]')
    $text=[regex]::Replace($text,'(?i)(https?://)[^/\s"''<>]*@','$1[redacted]@')
    $text=[regex]::Replace($text,'(?i)(https?://[^\s"''<>?#]+)[?#][^\s"''<>]*','$1?[redacted]')
    $text=[regex]::Replace($text,'(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/-]+=*','$1 [redacted]')
    $text=[regex]::Replace($text,'\bsk-[A-Za-z0-9_-]{8,}','[redacted]')
    $text=[regex]::Replace($text,'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+','[redacted]')
    if ($text.Length -gt 3000) {$text=$text.Substring(0,3000)+' [truncated]'}
    return $text
}

function Get-Property($Object,[string]$Name) {
    if ($null -eq $Object) {return $null}
    if ($Object -is [System.Collections.IDictionary]) {return $Object[$Name]}
    $property=$Object.PSObject.Properties[$Name]
    if ($null -ne $property) {return $property.Value}
    return $null
}

function Get-RoutingConfig($Value) {
    $result=[ordered]@{}
    foreach ($key in @('model','model_provider','profile','openai_base_url','chatgpt_base_url')) {
        $item=Get-Property $Value $key
        if ($null -ne $item) {$result[$key]=Protect-RoutingText $item}
    }
    $providers=Get-Property $Value 'model_providers'
    if ($providers) {
        $result['model_providers']=[ordered]@{}
        foreach ($entry in @($providers.PSObject.Properties | Where-Object MemberType -eq NoteProperty | Select-Object -First 40)) {
            $provider=[ordered]@{}
            foreach ($key in @('name','base_url','wire_api','requires_openai_auth','supports_websockets')) {
                $item=Get-Property $entry.Value $key
                if ($null -ne $item) {$provider[$key]=if($item -is [bool]){$item}else{Protect-RoutingText $item}}
            }
            $result['model_providers'][(Protect-RoutingText $entry.Name)]=$provider
        }
    }
    $profiles=Get-Property $Value 'profiles'
    if ($profiles) {
        $result['profiles']=[ordered]@{}
        foreach ($entry in @($profiles.PSObject.Properties | Where-Object MemberType -eq NoteProperty | Select-Object -First 40)) {
            # Profiles may contain provider selections, but never export tools,
            # instructions, auth values, MCP environment or conversation text.
            $copy=[pscustomobject]@{}
            foreach ($key in @('model','model_provider','openai_base_url','chatgpt_base_url','model_providers')) {
                $item=Get-Property $entry.Value $key
                if ($null -ne $item) {$copy | Add-Member -NotePropertyName $key -NotePropertyValue $item}
            }
            $result['profiles'][(Protect-RoutingText $entry.Name)]=Get-RoutingConfig $copy
        }
    }
    return $result
}

function Get-LayerMetadata($Value) {
    $result=[ordered]@{}
    foreach ($key in @('type','file','profile','dotCodexFolder','domain','key','id','name')) {
        $item=Get-Property $Value $key
        if ($null -ne $item) {$result[$key]=Protect-RoutingText $item}
    }
    return $result
}

function Get-RoutingRequirements($Value) {
    $result=[ordered]@{}
    foreach ($key in @('modelProvider','enforceResidency')) {
        $item=Get-Property $Value $key
        if ($null -ne $item -and $item -is [string]) {$result[$key]=Protect-RoutingText $item}
    }
    $providers=Get-Property $Value 'modelProviders'
    if ($providers) {$result['modelProviders']=(Get-RoutingConfig ([pscustomobject]@{model_providers=$providers}))['model_providers']}
    return $result
}

function Get-ConfigEvidence($Value,[string]$Cwd) {
    $origins=[ordered]@{}
    $raw=Get-Property $Value 'origins'
    foreach ($entry in @($raw.PSObject.Properties | Where-Object MemberType -eq NoteProperty)) {
        if ($entry.Name -match '^(model|model_provider|model_providers|profile|profiles|openai_base_url|chatgpt_base_url)(\.|$)') {
            $origins[(Protect-RoutingText $entry.Name)]=@{source=(Get-LayerMetadata (Get-Property $entry.Value 'name'));version=(Protect-RoutingText (Get-Property $entry.Value 'version'))}
        }
    }
    $layers=@(foreach ($layer in @(Get-Property $Value 'layers')) {
        if ($null -ne $layer) {
            @{source=(Get-LayerMetadata (Get-Property $layer 'name'));version=(Protect-RoutingText (Get-Property $layer 'version'));disabled_reason=(Protect-RoutingText (Get-Property $layer 'disabledReason'));routing=(Get-RoutingConfig (Get-Property $layer 'config'))}
        }
    })
    return @{cwd=$Cwd;config=(Get-RoutingConfig (Get-Property $Value 'config'));origins=$origins;layers=$layers}
}

function Split-NativeCommandLine([string]$Value) {
    if (-not ('CodexRoutingReport.NativeArguments' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace CodexRoutingReport {
    public static class NativeArguments {
        [DllImport("shell32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        static extern IntPtr CommandLineToArgvW(string text, out int count);
        [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr memory);
        public static string[] Split(string text) {
            int count; IntPtr memory=CommandLineToArgvW(text,out count);
            if(memory==IntPtr.Zero) throw new System.ComponentModel.Win32Exception();
            try {var values=new string[count];for(int i=0;i<count;i++)values[i]=Marshal.PtrToStringUni(Marshal.ReadIntPtr(memory,i*IntPtr.Size));return values;}
            finally {LocalFree(memory);}
        }
    }
}
'@
    }
    return [CodexRoutingReport.NativeArguments]::Split($Value)
}

function Get-ProcessRouting($ProcessInfo) {
    $values=@(Split-NativeCommandLine ([string]$ProcessInfo.CommandLine))
    $overrides=@();$chosenProfile=$null;$workingDirectory=$null;$isServer=$false
    for ($index=1;$index -lt $values.Count;$index++) {
        $item=$values[$index];$override=$null
        if ($item -eq 'app-server') {$isServer=$true}
        if ($item -in @('-c','--config') -and $index+1 -lt $values.Count) {$index++;$override=$values[$index]}
        elseif ($item.StartsWith('--config=')) {$override=$item.Substring(9)}
        elseif ($item -in @('-p','--profile') -and $index+1 -lt $values.Count) {$index++;$chosenProfile=$values[$index]}
        elseif ($item.StartsWith('--profile=')) {$chosenProfile=$item.Substring(10)}
        elseif ($item -in @('-C','--cd') -and $index+1 -lt $values.Count) {$index++;$workingDirectory=$values[$index]}
        if ($override -and $override -match '^\s*((?:model_provider|openai_base_url|chatgpt_base_url)|model_providers\.[^.]+\.(?:name|base_url|wire_api|requires_openai_auth|supports_websockets))\s*=') {
            $overrides+=Protect-RoutingText $override
        }
    }
    return @{pid=[int]$ProcessInfo.ProcessId;parent_pid=[int]$ProcessInfo.ParentProcessId;executable=[string]$ProcessInfo.ExecutablePath;app_server=$isServer;profile=(Protect-RoutingText $chosenProfile);cwd=(Protect-RoutingText $workingDirectory);routing_overrides=$overrides}
}

function Quote-NativeArgument([string]$Value) {
    return '"'+[regex]::Replace([regex]::Replace($Value,'(\\*)"','$1$1\"'),'(\\+)$','$1$1')+'"'
}

function Write-RpcLine($Session,[string]$Value) {
    # Windows PowerShell may choose a BOM-producing console encoding for
    # Process.StandardInput. JSONL must always start with a JSON token.
    $bytes=$script:Utf8.GetBytes($Value+"`n")
    $Session.input.Write($bytes,0,$bytes.Length)
    $Session.input.Flush()
}

function Invoke-ReadRpc($Session,[string]$Method,$Parameters,[int]$TimeoutSeconds=12) {
    if ($Method -notin @('initialize','config/read','configRequirements/read','thread/list','thread/read')) {throw 'Only read RPC methods are permitted.'}
    if ($Session.broken -or [DateTime]::UtcNow -ge $Session.deadline) {throw 'The diagnostic RPC session is no longer available.'}
    $Session.serial++
    $identifier=$Session.serial
    $request=@{id=$identifier;method=$Method;params=$Parameters}|ConvertTo-Json -Depth 15 -Compress
    Write-RpcLine $Session $request
    $deadline=[DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    if ($Session.deadline -lt $deadline) {$deadline=$Session.deadline}
    for ($iteration=0;$iteration -lt 100;$iteration++) {
        $remaining=[int][Math]::Max(1,($deadline-[DateTime]::UtcNow).TotalMilliseconds)
        if ([DateTime]::UtcNow -ge $deadline) {$Session.broken=$true;throw ('Native RPC timeout: '+$Method)}
        $read=$Session.process.StandardOutput.ReadLineAsync()
        if (-not $read.Wait($remaining)) {$Session.broken=$true;throw ('Native RPC timeout: '+$Method)}
        $line=$read.Result
        if ($null -eq $line) {$Session.broken=$true;throw ('Native RPC closed: '+$Method)}
        if ($line.Length -gt 8388608) {$Session.broken=$true;throw 'Native RPC response exceeded the diagnostic limit.'}
        try {$response=$line|ConvertFrom-Json} catch {continue}
        if ((Get-Property $response 'id') -ne $identifier) {continue}
        $rpcError=Get-Property $response 'error'
        if ($rpcError) {throw ('Native RPC '+$Method+' failed: '+(Get-Property $rpcError 'code'))}
        return Get-Property $response 'result'
    }
    $Session.broken=$true;throw ('Native RPC notification limit: '+$Method)
}

function Start-ReadSession([string]$Executable,[string[]]$Prefix=@(),[string]$CodexRoot='') {
    $start=New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName=$Executable
    $start.Arguments=(@($Prefix)+@('app-server','--listen','stdio://') | ForEach-Object {Quote-NativeArgument $_}) -join ' '
    $start.UseShellExecute=$false;$start.CreateNoWindow=$true
    $start.RedirectStandardInput=$true;$start.RedirectStandardOutput=$true;$start.RedirectStandardError=$true
    $start.StandardOutputEncoding=$script:Utf8;$start.StandardErrorEncoding=$script:Utf8
    if ($CodexRoot) {$start.EnvironmentVariables['CODEX_HOME']=$CodexRoot}
    $process=New-Object System.Diagnostics.Process
    $process.StartInfo=$start
    # .NET Framework creates an auto-flushing stdin writer during Start(). Its
    # default console UTF-8 encoding can emit a BOM before BaseStream is read.
    $previousEncoding=[Console]::InputEncoding
    try {
        [Console]::InputEncoding=$script:Utf8
        if (-not $process.Start()) {throw 'Could not start the diagnostic Codex process.'}
        $inputStream=$process.StandardInput.BaseStream
    } finally {[Console]::InputEncoding=$previousEncoding}
    return @{process=$process;input=$inputStream;stderr=$process.StandardError.ReadToEndAsync();serial=0;deadline=[DateTime]::UtcNow.AddSeconds(40);broken=$false}
}

function Stop-ReadSession($Session) {
    if ($null -eq $Session) {return}
    $process=$Session.process
    try {
        $Session.input.Close()
        if (-not $process.WaitForExit(1500)) {$process.Kill();[void]$process.WaitForExit(1500)}
    } finally {$process.Dispose()}
}

function Read-RelayStatus([int]$Port) {
    $request=[Net.HttpWebRequest]::Create('http://127.0.0.1:'+ $Port +'/api/status')
    $request.Proxy=$null;$request.AllowAutoRedirect=$false;$request.Timeout=3000;$request.ReadWriteTimeout=3000
    $response=$null;$reader=$null
    try {
        $response=$request.GetResponse();$reader=New-Object IO.StreamReader($response.GetResponseStream(),$script:Utf8)
        $buffer=New-Object char[] 4096;$text=New-Object Text.StringBuilder
        while (($count=$reader.Read($buffer,0,$buffer.Length)) -gt 0) {
            if ($text.Length+$count -gt 1048576) {throw 'Relay status exceeded the diagnostic limit.'}
            [void]$text.Append($buffer,0,$count)
        }
        return $text.ToString()|ConvertFrom-Json
    } finally {if($reader){$reader.Dispose()};if($response){$response.Dispose()}}
}

function Get-ThreadMetadata($Value) {
    $result=[ordered]@{}
    foreach ($key in @('id','modelProvider','cwd','path','cliVersion','createdAt','updatedAt','source')) {
        $item=Get-Property $Value $key
        if ($null -ne $item) {
            # A subagent source can contain a prompt. Keep only its type.
            $result[$key]=if($key -eq 'source' -and $item -isnot [string]){'structured_source'}else{Protect-RoutingText $item}
        }
    }
    return $result
}

function Invoke-RoutingCollection([string]$RequestedThread='',[string]$Destination='',[int]$Port=47844) {
    if ($RequestedThread) {
        $RequestedThread=$RequestedThread -replace '^codex://threads/',''
        if ($RequestedThread -notmatch '^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$') {throw 'Invalid thread ID or Codex thread link.'}
    }
    if (-not $Destination) {
        $parent=if($env:CODEX_ROUTING_REPORT_SCRIPT){Split-Path -Parent $env:CODEX_ROUTING_REPORT_SCRIPT}else{(Get-Location).Path}
        $Destination=Join-Path $parent ('CodexConfigReport-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'-'+[guid]::NewGuid().ToString('N').Substring(0,6))
    }
    [void](New-Item -ItemType Directory -Path $Destination)
    Write-Host '正在读取本机入口和 Codex 启动参数…'
    $report=[ordered]@{collected_at=[DateTimeOffset]::Now.ToString('o');scope='Separate read-only diagnostic process; not attached to the existing app-server or its in-memory sessions.';requested_thread=$RequestedThread;processes=@();environment=@{};relay=$null;threads=@();configurations=@();requirements=@{};errors=@()}
    foreach ($key in @('CODEX_HOME','CODEX_CLI_PATH','OPENAI_BASE_URL')) {
        $item=[Environment]::GetEnvironmentVariable($key)
        if ($item) {$report.environment[$key]=Protect-RoutingText $item}
    }
    $report.environment['scope']='Collector environment only; the running ChatGPT process environment was not read.'
    $codexRoot=if($env:CODEX_HOME){$env:CODEX_HOME}else{Join-Path $env:USERPROFILE '.codex'}
    try {
        $status=Read-RelayStatus $Port
        $profile=Get-Property $status 'profile'
        $report.relay=@{version=$status.version;capture_enabled=$status.capture_enabled;capture_errors=$status.capture_errors;request_count=$status.storage.count;connection_state=$status.connection_state;config_path=$profile.config_path;provider=$profile.provider;base_url=(Protect-RoutingText $profile.base_url);upstream=(Protect-RoutingText $profile.upstream)}
        if ($profile.config_path -and (Test-Path -LiteralPath $profile.config_path)) {$codexRoot=Split-Path -Parent $profile.config_path}
    } catch {$report.errors+=@{stage='relay_status';type=$_.Exception.GetType().Name}}
    try {
        $processes=@(Get-CimInstance Win32_Process -Filter "Name='codex.exe'" | Where-Object {$_.ExecutablePath} | Sort-Object CreationDate -Descending | Select-Object -First 20)
        $report.processes=@(foreach($item in $processes){Get-ProcessRouting $item})
    } catch {$report.errors+=@{stage='process_inventory';type=$_.Exception.GetType().Name}}
    $report['codex_root']=$codexRoot
    $candidate=@($report.processes | Where-Object app_server | Select-Object -First 1)
    $executable=if($candidate.Count){$candidate[0].executable}elseif($env:CODEX_CLI_PATH){$env:CODEX_CLI_PATH}else{$null}
    if (-not $executable -or -not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        $command=Get-Command codex.exe -ErrorAction SilentlyContinue
        $executable=if($command){$command.Source}else{$null}
    }
    $report['diagnostic_executable']=$executable
    $report['session_flags_replayed']=$false
    $report['limits']=@('Thread provider is stored metadata, not proof of the current request URL.','Config RPC resolves current on-disk configuration; existing session memory and RPC overrides may differ.','Existing process flags are reported but not replayed; requirements may also constrain provider selection.','Credentials, conversation text, instructions and MCP environment are excluded. Native startup may write its own logs/cache.')
    $session=$null
    try {
        if (-not $executable) {throw 'Start ChatGPT on this PC, then run this collector again.'}
        Write-Host '正在查询会话 provider 和分层配置，最多等待约 40 秒…'
        $session=Start-ReadSession -Executable $executable -CodexRoot $codexRoot
        [void](Invoke-ReadRpc $session 'initialize' @{clientInfo=@{name='codex_routing_report';title='Read-only routing report';version='1'}})
        Write-RpcLine $session '{"method":"initialized"}'
        try {
            if ($RequestedThread) {
                $value=Invoke-ReadRpc $session 'thread/read' @{threadId=$RequestedThread;includeTurns=$false}
                $report.threads=@(Get-ThreadMetadata $value.thread)
            } else {
                $value=Invoke-ReadRpc $session 'thread/list' @{limit=10;sortKey='updated_at';archived=$false;useStateDbOnly=$true}
                $report.threads=@(foreach($thread in @($value.data)){Get-ThreadMetadata $thread})
            }
        } catch {$report.errors+=@{stage='thread_metadata';message=(Protect-RoutingText $_.Exception.Message)}}
        $directories=@('')+@($report.threads | ForEach-Object {$_.cwd} | Where-Object {$_} | Select-Object -Unique -First 3)
        foreach ($directory in $directories) {
            try {
                $parameters=@{includeLayers=$true}
                if ($directory) {$parameters.cwd=$directory}
                $value=Invoke-ReadRpc $session 'config/read' $parameters
                $report.configurations+=Get-ConfigEvidence $value $directory
            } catch {$report.errors+=@{stage='config_read';cwd=$directory;message=(Protect-RoutingText $_.Exception.Message)}}
        }
        try {
            $value=Invoke-ReadRpc $session 'configRequirements/read' @{}
            $report.requirements=Get-RoutingRequirements $value.requirements
        } catch {$report.errors+=@{stage='requirements';message=(Protect-RoutingText $_.Exception.Message)}}
    } catch {$report.errors+=@{stage='native_start';message=(Protect-RoutingText $_.Exception.Message)}}
    finally {Stop-ReadSession $session}
    $json=$report|ConvertTo-Json -Depth 30
    [IO.File]::WriteAllText((Join-Path $Destination 'report.json'),$json,$script:Utf8)
    $lines=@(
        'Codex 路由只读诊断'
        ('时间：'+$report.collected_at)
        ('Codex 目录：'+$codexRoot)
        ('会话元数据：'+$report.threads.Count+' 条')
        ('配置来源：'+$report.configurations.Count+' 组')
        ('异常：'+$report.errors.Count+' 项')
        ''
        '这是独立诊断进程的读取结果，不等同于原窗口的内存配置。'
        '不会发送消息、调用模型、修改接入配置或停止现有程序。'
        '原生启动可能产生其自身日志或缓存。'
        '报告不包含会话正文、认证值、MCP 环境或指令内容。'
        ''
        '会话 provider：'
    )
    foreach($thread in $report.threads){$lines+=([string]$thread.id+'  '+[string]$thread.modelProvider)}
    [IO.File]::WriteAllLines((Join-Path $Destination 'summary.txt'),$lines,$script:Utf8)
    Write-Host ('采集完成：'+$Destination)
    Write-Host ('会话 '+$report.threads.Count+' 条；配置来源 '+$report.configurations.Count+' 组；异常 '+$report.errors.Count+' 项。')
    Write-Host '请发送该目录中的 report.json 和 summary.txt。'
    return $report
}

if ($LoadOnly) {return}
try {[void](Invoke-RoutingCollection -RequestedThread $ThreadId -Destination $OutputDirectory -Port $RelayPort)}
catch {Write-Host ('采集失败：'+(Protect-RoutingText $_.Exception.Message));exit 1}
