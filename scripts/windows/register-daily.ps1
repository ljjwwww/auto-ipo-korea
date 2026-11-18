#Requires -Version 5.1
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Distribution,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$LinuxUser,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$ProjectPath,
    [ValidatePattern('^([01][0-9]|2[0-3]):[0-5][0-9]$')][string]$At = '19:00',
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')][string]$TaskName = 'AutoIpoKorea-Daily',
    [switch]$AllowApi,
    [ValidateRange(1,60)][int]$TestAfterMinutes,
    [datetime]$TestAt,
    [switch]$TestOnly
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$legacyMarker = 'Managed by auto-ipo-korea daily scheduler v1; no notification sending.'
$marker = 'Managed by auto-ipo-korea daily scheduler v2; logged WSL launcher.'
foreach ($value in @($Distribution, $LinuxUser, $ProjectPath)) {
    if ($value -match '["`\\\x00-\x1f\x7f]') { throw 'Invalid scheduler parameter.' }
}
if ($Distribution.StartsWith('-') -or $LinuxUser.StartsWith('-') -or
    -not $ProjectPath.StartsWith('/') -or $ProjectPath -eq '/') { throw 'Invalid scheduler parameter.' }
$ProjectPath = $ProjectPath.TrimEnd('/')
function Join-NativeArguments([string[]]$Tokens) {
    return (($Tokens | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join ' ')
}
$legacyTokens = @('-d', $Distribution, '-u', $LinuxUser, '--cd', $ProjectPath, '--exec', "$ProjectPath/.venv/bin/python", '-m', 'ipo_monitor', 'daily', '--config', "$ProjectPath/config/daily.toml")
if ($AllowApi) { $legacyTokens += '--allow-api' }
# Reproduce v1 only for exact ownership verification; never execute this string.
$legacyArguments = ($legacyTokens | ForEach-Object { '"' + $_ + '"' }) -join ' '
$legacyExecutable = Join-Path $env:WINDIR 'System32\wsl.exe'
$installRoot = Join-Path $env:LOCALAPPDATA 'AutoIpoKorea'
$runner = Join-Path $installRoot 'run-daily.ps1'
$executable = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
$tokens = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $runner,
            '-Distribution', $Distribution, '-LinuxUser', $LinuxUser, '-ProjectPath', $ProjectPath)
if ($AllowApi) { $tokens += '-AllowApi' }
$arguments = Join-NativeArguments $tokens
function Test-PrincipalOwner($Principal, $Identity) {
    try {
        $account = [string]$Principal.UserId
        if ($account.StartsWith('S-1-')) {
            $sid = New-Object Security.Principal.SecurityIdentifier($account)
        } else {
            $sid = (New-Object Security.Principal.NTAccount($account)).Translate([Security.Principal.SecurityIdentifier])
        }
        # CIM returns numeric enum values: Interactive=3, Limited=0.
        return $sid.Value -eq $Identity.User.Value -and [int]$Principal.LogonType -eq 3 -and [int]$Principal.RunLevel -eq 0
    } catch { return $false }
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$existing = @(Get-ScheduledTask -TaskPath '\' | Where-Object { $_.TaskName -ceq $TaskName })
if ($existing.Count -gt 1) { throw 'Ambiguous scheduled task identity.' }
if ($existing.Count -eq 1) {
    $task = $existing[0]; $actions = @($task.Actions)
    $legacy = $task.Description -ceq $legacyMarker -and $actions.Count -eq 1 -and $actions[0].Execute -ieq $legacyExecutable -and $actions[0].Arguments -ceq $legacyArguments
    $current = $task.Description -ceq $marker -and $actions.Count -eq 1 -and $actions[0].Execute -ieq $executable -and $actions[0].Arguments -ceq $arguments
    if ((-not $legacy -and -not $current) -or
        -not [string]::IsNullOrEmpty($actions[0].WorkingDirectory) -or
        -not (Test-PrincipalOwner $task.Principal $identity)) {
        throw 'Existing task is not an exact managed action for this user; refusing replacement.'
    }
    if ($task.State -eq 'Running') { throw 'Task is running; refusing changes.' }
}
# Fixed-time one-off registration must not touch the daily task or launcher.
if ($TestOnly) {
    if ($existing.Count -ne 1 -or -not $current -or
        -not $PSBoundParameters.ContainsKey('TestAt') -or
        $PSBoundParameters.ContainsKey('TestAfterMinutes')) { throw 'TestOnly requires a verified v2 daily task and TestAt.' }
    if ($TestAt -le (Get-Date) -or $TestAt.Date -ne [datetime]::Today) { throw 'TestAt must be a future time today.' }
    $dailyBefore = Export-ScheduledTask -TaskName $TaskName -TaskPath '\'
    $testName = 'AutoIpoKorea-Test-' + $TestAt.ToString('yyyyMMddHHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8)
    if ($PSCmdlet.ShouldProcess("\$testName", 'Register today-only test; leave daily task unchanged')) {
        $testTrigger = New-ScheduledTaskTrigger -Once -At $TestAt
        $testTrigger.EndBoundary = $TestAt.Date.AddDays(1).AddSeconds(-1).ToString('s')
        $testDefinition = New-ScheduledTask -Action $task.Actions -Trigger $testTrigger -Principal $task.Principal -Settings $task.Settings -Description ($marker + ' One-time scheduled validation.')
        Register-ScheduledTask -TaskName $testName -TaskPath '\' -InputObject $testDefinition | Out-Null
        $dailyAfter = Export-ScheduledTask -TaskName $TaskName -TaskPath '\'
        if ($dailyBefore -cne $dailyAfter) { throw 'Unexpected daily task change.' }
        $check = Get-ScheduledTask -TaskName $testName -TaskPath '\'
        $info = Get-ScheduledTaskInfo -TaskName $testName -TaskPath '\'
        $dailyInfo = Get-ScheduledTaskInfo -TaskName $TaskName -TaskPath '\'
        [pscustomobject]@{ TestTask=$testName; TestAt=$check.Triggers[0].StartBoundary; EndBoundary=$check.Triggers[0].EndBoundary; NextRun=$info.NextRunTime.ToString('s'); DailyNextRun=$dailyInfo.NextRunTime.ToString('s'); DailyUnchanged=$true; State=[string]$check.State } | ConvertTo-Json -Compress
    }
    return
}
if ($PSBoundParameters.ContainsKey('TestAt')) { throw 'TestAt requires TestOnly.' }
$sourceRunner = Join-Path $PSScriptRoot 'run-daily.ps1'
if (-not (Test-Path -LiteralPath $sourceRunner -PathType Leaf)) { throw 'Launcher source is missing.' }
$action = New-ScheduledTaskAction -Execute $executable -Argument $arguments
$time = [datetime]::Today.AddHours([int]$At.Substring(0,2)).AddMinutes([int]$At.Substring(3,2))
$trigger = New-ScheduledTaskTrigger -Daily -At $time
$principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Priority 4
Write-Output "Daily $At Windows-local time; time zone: $((Get-TimeZone).Id); API: $($AllowApi.IsPresent)"
if ($PSCmdlet.ShouldProcess("\$TaskName", 'Install logged launcher and register verified daily task')) {
    New-Item -ItemType Directory -Path $installRoot -Force | Out-Null
    Copy-Item -LiteralPath $sourceRunner -Destination $runner -Force
    $definition = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description $marker
    # Replacement is allowed only after exact managed-action/user validation above.
    if ($existing.Count -eq 1) {
        Register-ScheduledTask -TaskName $TaskName -TaskPath '\' -InputObject $definition -Force | Out-Null
    } else {
        Register-ScheduledTask -TaskName $TaskName -TaskPath '\' -InputObject $definition | Out-Null
    }
    Write-Output "Registered \$TaskName. No immediate execution."
    if ($PSBoundParameters.ContainsKey('TestAfterMinutes')) {
        # Compute the trigger AFTER installation/registration, not before diagnostics.
        $testAt = (Get-Date).AddMinutes($TestAfterMinutes)
        $testName = 'AutoIpoKorea-Test-' + (Get-Date -Format 'yyyyMMddHHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8)
        $testTrigger = New-ScheduledTaskTrigger -Once -At $testAt
        $testDefinition = New-ScheduledTask -Action $action -Trigger $testTrigger -Principal $principal -Settings $settings -Description ($marker + ' One-time scheduled validation.')
        Register-ScheduledTask -TaskName $testName -TaskPath '\' -InputObject $testDefinition | Out-Null
        [pscustomobject]@{TestTask=$testName;TestAt=$testAt.ToString('o');RegisteredAt=(Get-Date).ToString('o');DailyTask=$TaskName;DailyAt=$At;LogDirectory=(Join-Path $installRoot 'logs')} | ConvertTo-Json -Compress
    }
}
