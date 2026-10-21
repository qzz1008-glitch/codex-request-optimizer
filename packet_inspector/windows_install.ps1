param(
    [ValidateSet('Status', 'Install', 'Remove', 'EnableStartup', 'DisableStartup')][string]$Action = 'Status',
    [string]$Executable,
    [string]$DataDirectory,
    [string]$ConfigPath,
    [ValidateRange(1, 65535)][int]$Port = 47844
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$taskName = 'Codex Packet Inspector'
$taskExpectedDirectory = [IO.Path]::GetFullPath((Join-Path $env:USERPROFILE '.codex-packet-inspector'))
if ([IO.Path]::GetFullPath($DataDirectory) -ne $taskExpectedDirectory) {
    throw 'Installation is limited to the default per-user data directory.'
}
$taskBinDirectory = Join-Path $taskExpectedDirectory 'bin'
$taskInstalledExecutable = Join-Path $taskBinDirectory 'CodexPacketInspector.exe'
$taskShortcut = Join-Path ([Environment]::GetFolderPath('Programs')) 'Codex Request Optimizer.lnk'
$taskLegacyShortcut = Join-Path ([Environment]::GetFolderPath('Programs')) 'Codex Packet Inspector.lnk'
$taskExisting = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($taskExisting -and (@($taskExisting.Actions).Count -ne 1 -or $taskExisting.Actions[0].Execute -ne $taskInstalledExecutable)) {
    throw 'A task with this name belongs to another installation and was left unchanged.'
}
if ($Action -eq 'Install') {
    $taskSource = (Resolve-Path -LiteralPath $Executable).Path
    New-Item -ItemType Directory -Path $taskBinDirectory -Force | Out-Null
    if ($taskSource -ne $taskInstalledExecutable) {
        $taskCopyRequired = -not (Test-Path -LiteralPath $taskInstalledExecutable)
        if (-not $taskCopyRequired) {
            $taskCopyRequired = (Get-FileHash -LiteralPath $taskSource).Hash -ne (Get-FileHash -LiteralPath $taskInstalledExecutable).Hash
        }
        if ($taskCopyRequired) { Copy-Item -LiteralPath $taskSource -Destination $taskInstalledExecutable -Force }
    }
    # Installation preserves an existing login task; enabling startup is an explicit setting.
    $taskShell = New-Object -ComObject WScript.Shell
    $taskLink = $taskShell.CreateShortcut($taskShortcut)
    $taskLink.TargetPath = $taskInstalledExecutable
    $taskLink.Arguments = '--data-dir "' + $taskExpectedDirectory + '" --config "' + $ConfigPath + '"'
    $taskLink.WorkingDirectory = $taskBinDirectory
    $taskLink.Save()
    if (Test-Path -LiteralPath $taskLegacyShortcut -PathType Leaf) {
        $taskOldLink = $taskShell.CreateShortcut($taskLegacyShortcut)
        if ($taskOldLink.TargetPath -eq $taskInstalledExecutable) {
            Remove-Item -LiteralPath $taskLegacyShortcut
        }
    }
    # The current portable server keeps running; no active requests are stopped.
    $taskExisting = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
} elseif ($Action -eq 'EnableStartup') {
    if (-not (Test-Path -LiteralPath $taskInstalledExecutable) -or
        [IO.Path]::GetFullPath($Executable) -ne [IO.Path]::GetFullPath($taskInstalledExecutable)) {
        throw 'Enable startup from the installed application.'
    }
    $taskIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $taskArguments = '--background --tray --data-dir "' + $taskExpectedDirectory + '" --config "' + $ConfigPath + '"'
    $taskAction = New-ScheduledTaskAction -Execute $taskInstalledExecutable -Argument $taskArguments -WorkingDirectory $taskBinDirectory
    $taskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $taskIdentity
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId $taskIdentity -LogonType Interactive -RunLevel Limited
    $taskSettings = New-ScheduledTaskSettingsSet -Hidden -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $taskTrigger -Principal $taskPrincipal -Settings $taskSettings -Description 'Start Codex Request Optimizer when the user signs in.' -Force | Out-Null
    $taskExisting = Get-ScheduledTask -TaskName $taskName
} elseif ($Action -eq 'DisableStartup') {
    # Cancel future login launches. Leave the running application alone.
    if ($taskExisting) { $taskExisting = Disable-ScheduledTask -TaskName $taskName }
} elseif ($Action -eq 'Remove') {
    if ($taskExisting) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false; $taskExisting = $null }
    # Delete only this exact shortcut. Keep configuration, captures and the running EXE.
    if (Test-Path -LiteralPath $taskShortcut) { Remove-Item -LiteralPath $taskShortcut }
}
@{
    supported = $true
    installed = [bool](Test-Path -LiteralPath $taskInstalledExecutable)
    executable = $taskInstalledExecutable
    shortcut = (Test-Path -LiteralPath $taskShortcut)
    login_start = [bool]($taskExisting -and $taskExisting.State -ne 'Disabled')
} | ConvertTo-Json -Compress
