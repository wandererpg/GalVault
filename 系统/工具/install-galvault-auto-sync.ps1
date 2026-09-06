[CmdletBinding()]
param(
    [ValidateRange(1, 1440)]
    [int]$IntervalMinutes = 10,
    [switch]$Uninstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$taskName = "GalVault Auto Upload"
$vaultRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$syncScript = Join-Path $vaultRoot "系统\工具\galvault-auto-sync.ps1"
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -ne $existing) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Output "Removed scheduled task: $taskName"
    }
    else {
        Write-Output "Scheduled task not found: $taskName"
    }
    exit 0
}

if (-not (Test-Path -LiteralPath $syncScript -PathType Leaf)) {
    throw "Sync script not found: $syncScript"
}

$powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
$action = New-ScheduledTaskAction `
    -Execute $powershell `
    -Argument "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$syncScript`" -Quiet"

$firstRun = (Get-Date).AddMinutes(1)
$repeatTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At $firstRun `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $userId

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger @($logonTrigger, $repeatTrigger) `
    -Settings $settings `
    -Principal $principal `
    -Description "Commit and upload non-ignored GalVault changes over SSH without force-pushing." `
    -Force | Out-Null

Write-Output "Installed scheduled task '$taskName' for $userId."
Write-Output "It runs at logon and every $IntervalMinutes minute(s) while the user is logged in."
Write-Output "Log: $env:LOCALAPPDATA\GalVault\auto-sync.log"
