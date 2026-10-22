param(
    [string]$SourceExecutable = (Join-Path $PSScriptRoot 'CodexPacketInspector.exe'),
    [string]$DataDir = (Join-Path $env:USERPROFILE '.codex-packet-inspector'),
    [string]$ConfigPath = (Join-Path $env:USERPROFILE '.codex\config.toml'),
    [ValidateRange(1024,65535)][int]$Port
)
$ErrorActionPreference = 'Stop'
$taskUpgrade = (Resolve-Path -LiteralPath $SourceExecutable).Path
if ([IO.Path]::GetFileName($taskUpgrade) -ne 'CodexPacketInspector.exe') {
    throw 'Use CodexPacketInspector.exe from the new release. The application includes its upgrade engine; do not overwrite a running entry manually.'
}
$taskData = [IO.Path]::GetFullPath($DataDir)
$taskConfig = [IO.Path]::GetFullPath($ConfigPath)
if ($taskData.Contains('"') -or $taskConfig.Contains('"')) { throw 'Invalid path.' }
$taskArguments = @('--upgrade', '--quiet', '--data-dir', ('"' + $taskData + '"'), '--config', ('"' + $taskConfig + '"'))
if ($PSBoundParameters.ContainsKey('Port')) { $taskArguments += @('--port', $Port) }
$taskChild = Start-Process -FilePath $taskUpgrade -ArgumentList $taskArguments -WindowStyle Hidden -Wait -PassThru
$taskResult = Join-Path $taskData 'upgrade-result.json'
if ($taskChild.ExitCode -ne 0) {
    $taskReason = 'Upgrade did not complete. Existing requests were not forcibly stopped.'
    if (Test-Path -LiteralPath $taskResult) { $taskReason = (Get-Content -LiteralPath $taskResult -Raw | ConvertFrom-Json).error }
    throw $taskReason
}
Get-Content -LiteralPath $taskResult -Raw | ConvertFrom-Json | Select-Object state,version,port,entry_migrated
