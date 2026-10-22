param(
    [string]$Executable = (Join-Path $PSScriptRoot 'CodexRequestOptimizer-Upgrade.exe'),
    [string]$DataDir = (Join-Path $env:USERPROFILE '.codex-packet-inspector'),
    [string]$ConfigPath = (Join-Path $env:USERPROFILE '.codex\config.toml'),
    [ValidateRange(1024,65535)][int]$Port
)
$ErrorActionPreference = 'Stop'
$taskUpgrade = (Resolve-Path -LiteralPath $Executable).Path
if ([IO.Path]::GetFileName($taskUpgrade) -ne 'CodexRequestOptimizer-Upgrade.exe') {
    throw 'Use CodexRequestOptimizer-Upgrade.exe from the new release. Do not overwrite the stable entry with an application EXE.'
}
$taskData = [IO.Path]::GetFullPath($DataDir)
$taskConfig = [IO.Path]::GetFullPath($ConfigPath)
if ($taskData.Contains('"') -or $taskConfig.Contains('"')) { throw 'Invalid path.' }
$taskArguments = @('--quiet', '--data-dir', ('"' + $taskData + '"'), '--config', ('"' + $taskConfig + '"'))
if ($PSBoundParameters.ContainsKey('Port')) { $taskArguments += @('--port', $Port) }
$taskChild = Start-Process -FilePath $taskUpgrade -ArgumentList $taskArguments -WindowStyle Hidden -Wait -PassThru
$taskResult = Join-Path $taskData 'upgrade-result.json'
if ($taskChild.ExitCode -ne 0) {
    $taskReason = 'Installation did not complete. Existing requests were not forcibly stopped.'
    if (Test-Path -LiteralPath $taskResult) { $taskReason = (Get-Content -LiteralPath $taskResult -Raw | ConvertFrom-Json).error }
    throw $taskReason
}
Get-Content -LiteralPath $taskResult -Raw | ConvertFrom-Json | Select-Object state,version,port,entry_migrated
