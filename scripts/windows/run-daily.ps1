#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Distribution,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$LinuxUser,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$ProjectPath,
    [switch]$AllowApi,
    [switch]$Probe
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
foreach ($value in @($Distribution, $LinuxUser, $ProjectPath)) {
    if ($value -match '["`\\\x00-\x1f\x7f]') { throw 'Invalid launch parameter.' }
}
if ($Distribution.StartsWith('-') -or $LinuxUser.StartsWith('-') -or
    -not $ProjectPath.StartsWith('/') -or $ProjectPath -eq '/') { throw 'Invalid launch parameter.' }
$ProjectPath = $ProjectPath.TrimEnd('/')
# WSL treats a quoted leading option as a Linux command. Quote only whitespace.
function Join-NativeArguments([string[]]$Tokens) {
    return (($Tokens | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join ' ')
}
$tokens = @('-d', $Distribution, '-u', $LinuxUser, '--cd', $ProjectPath, '--exec', "$ProjectPath/.venv/bin/python", '-m', 'ipo_monitor.scheduler_entry')
if ($Probe) { $tokens += '--help' } else {
    $tokens += @('daily', '--config', "$ProjectPath/config/daily.toml")
    if ($AllowApi) { $tokens += '--allow-api' }
}
$arguments = Join-NativeArguments $tokens
$logRoot = Join-Path $env:LOCALAPPDATA 'AutoIpoKorea\logs'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$runName = (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
$prefix = Join-Path $logRoot $runName
$record = [ordered]@{ started_at=(Get-Date).ToString('o'); finished_at=$null; probe=$Probe.IsPresent; exit_code=$null; arguments=$arguments; stdout_file=($prefix+'.stdout.log'); stderr_file=($prefix+'.stderr.log'); startup_error=$null }
$record | ConvertTo-Json | Set-Content -LiteralPath ($prefix+'.run.json') -Encoding UTF8
$exitCode = 125
$process = New-Object System.Diagnostics.Process
try {
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = Join-Path $env:WINDIR 'System32\wsl.exe'
    $info.Arguments = $arguments
    $info.WorkingDirectory = $env:WINDIR
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $info.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $process.StartInfo = $info
    if (-not $process.Start()) { throw 'Native process did not start.' }
    # Read both pipes concurrently; neither stream can deadlock the other.
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $stdout.GetAwaiter().GetResult() | Set-Content -LiteralPath $record.stdout_file -Encoding UTF8
    $stderr.GetAwaiter().GetResult() | Set-Content -LiteralPath $record.stderr_file -Encoding UTF8
    $exitCode = $process.ExitCode
} catch {
    # Exception messages can contain process/environment details. Record type only.
    $record.startup_error = $_.Exception.GetType().FullName
} finally {
    $record.exit_code = $exitCode
    $record.finished_at = (Get-Date).ToString('o')
    $record | ConvertTo-Json | Set-Content -LiteralPath ($prefix+'.run.json') -Encoding UTF8
    $process.Dispose()
}
Write-Output "Launcher completed: exit=$exitCode; log=$prefix.run.json"
exit $exitCode
