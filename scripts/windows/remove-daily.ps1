#Requires -Version 5.1
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Distribution,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$LinuxUser,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$ProjectPath,
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')][string]$TaskName = 'AutoIpoKorea-Daily',
    [switch]$AllowApi
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
$legacyArguments = ($legacyTokens | ForEach-Object { '"' + $_ + '"' }) -join ' '
$legacyExecutable = Join-Path $env:WINDIR 'System32\wsl.exe'
$runner = Join-Path $env:LOCALAPPDATA 'AutoIpoKorea\run-daily.ps1'
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
if ($existing.Count -eq 0) { Write-Output "No task \$TaskName; nothing changed."; return }
if ($existing.Count -ne 1) { throw 'Ambiguous scheduled task identity.' }
$task = $existing[0]; $actions = @($task.Actions)
$legacy = $task.Description -ceq $legacyMarker -and $actions.Count -eq 1 -and $actions[0].Execute -ieq $legacyExecutable -and $actions[0].Arguments -ceq $legacyArguments
$knownMarker = $task.Description -ceq $marker -or $task.Description -ceq ($marker + ' One-time scheduled validation.')
$current = $knownMarker -and $actions.Count -eq 1 -and $actions[0].Execute -ieq $executable -and $actions[0].Arguments -ceq $arguments
if ((-not $legacy -and -not $current) -or
    -not [string]::IsNullOrEmpty($actions[0].WorkingDirectory) -or
    -not (Test-PrincipalOwner $task.Principal $identity)) {
    throw 'Existing task is not an exact managed action for this user; refusing removal.'
}
if ($task.State -eq 'Running') { throw 'Task is running; wait until it finishes before removing its schedule.' }
if ($PSCmdlet.ShouldProcess("\$TaskName", 'Remove verified current-user WSL task')) {
    Unregister-ScheduledTask -InputObject $task -Confirm:$false
    Write-Output "Removed \$TaskName. Local data, logs and credentials were not changed."
}
