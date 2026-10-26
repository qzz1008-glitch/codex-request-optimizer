param(
    [ValidateSet('Status', 'Install', 'Remove', 'EnableStartup', 'DisableStartup')][string]$Action = 'Status',
    [string]$Executable,
    [string]$DataDirectory,
    [string]$ConfigPath,
    [string]$IconPath,
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
function Get-OptimizerFileHash {
    param([string]$Path)
    $stream = [IO.File]::OpenRead($Path)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($algorithm.ComputeHash($stream)) }
    finally { $algorithm.Dispose(); $stream.Dispose() }
}
function Update-OwnedOptimizerShortcut {
    param([string]$Path, [string]$BinDirectory, [string]$DataDirectory, [string]$ConfigPath, [string]$IconPath)
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($Path)
    if (-not $link.TargetPath) { return $false }
    $target = [IO.Path]::GetFullPath($link.TargetPath)
    $canonical = Join-Path $BinDirectory 'CodexPacketInspector.exe'
    $versions = (Join-Path $BinDirectory 'versions') + [IO.Path]::DirectorySeparatorChar
    if ($target -ne $canonical -and -not ($target.StartsWith($versions, [StringComparison]::OrdinalIgnoreCase) -and
        [IO.Path]::GetFileName($target) -eq 'CodexPacketInspector.exe')) { return $false }
    foreach ($setting in @(@('data-dir', $DataDirectory), @('config', $ConfigPath))) {
        if ($link.Arguments -match ('--' + $setting[0] + '(?:=|\s+)(?:"([^"]+)"|(\S+))')) {
            $value = if ($Matches[1]) { $Matches[1] } else { $Matches[2] }
            if ([IO.Path]::GetFullPath($value) -ne [IO.Path]::GetFullPath($setting[1])) { return $false }
        }
    }
    $link.TargetPath = $canonical
    $link.Arguments = '--data-dir "' + $DataDirectory + '" --config "' + $ConfigPath + '"'
    $link.WorkingDirectory = $BinDirectory
    $link.IconLocation = $IconPath + ',0'
    $link.Save()
    return $true
}
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
            $taskCopyRequired = (Get-OptimizerFileHash -Path $taskSource) -ne (Get-OptimizerFileHash -Path $taskInstalledExecutable)
        }
        if ($taskCopyRequired) { Copy-Item -LiteralPath $taskSource -Destination $taskInstalledExecutable -Force }
    }
    # Installation preserves an existing login task; enabling startup is an explicit setting.
    $taskShell = New-Object -ComObject WScript.Shell
    $taskLink = $taskShell.CreateShortcut($taskShortcut)
    $taskLink.TargetPath = $taskInstalledExecutable
    $taskLink.Arguments = '--data-dir "' + $taskExpectedDirectory + '" --config "' + $ConfigPath + '"'
    $taskLink.WorkingDirectory = $taskBinDirectory
    if ($IconPath) {
        $taskIconSource = (Resolve-Path -LiteralPath $IconPath).Path
        $taskInstalledIcon = Join-Path $taskBinDirectory 'request-optimizer-logo.ico'
        $taskIconStaging = $taskInstalledIcon + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
        try {
            Copy-Item -LiteralPath $taskIconSource -Destination $taskIconStaging
            Move-Item -LiteralPath $taskIconStaging -Destination $taskInstalledIcon -Force
        } finally {
            if (Test-Path -LiteralPath $taskIconStaging) { Remove-Item -LiteralPath $taskIconStaging }
        }
        $taskLink.IconLocation = $taskInstalledIcon + ',0'
    }
    $taskLink.Save()
    if ($IconPath) {
        $taskPinned = Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar'
        if (Test-Path -LiteralPath $taskPinned -PathType Container) {
            Get-ChildItem -LiteralPath $taskPinned -File -Filter '*.lnk' | ForEach-Object {
                Update-OwnedOptimizerShortcut -Path $_.FullName -BinDirectory $taskBinDirectory -DataDirectory $taskExpectedDirectory -ConfigPath $ConfigPath -IconPath $taskInstalledIcon | Out-Null
            }
        }
    }
    Add-Type -Namespace CodexRequestOptimizer -Name ShellRefresh -MemberDefinition @'
        [System.Runtime.InteropServices.DllImport("shell32.dll")]
        public static extern void SHChangeNotify(uint eventId, uint flags, System.IntPtr first, System.IntPtr second);
'@
    [CodexRequestOptimizer.ShellRefresh]::SHChangeNotify(0x08000000, 0, [IntPtr]::Zero, [IntPtr]::Zero)
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
