#Requires -Version 5.1
$ErrorActionPreference='Stop'
foreach ($name in @('register-daily.ps1','remove-daily.ps1','run-daily.ps1')) {
    $tokens=$null;$errors=$null
    [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot $name),[ref]$tokens,[ref]$errors) | Out-Null
    if ($errors.Count) {throw "PowerShell syntax errors in $name"}
}
Write-Output 'PowerShell syntax: OK. Executing HELP ONLY probe; not daily collection.'
& (Join-Path $PSScriptRoot 'run-daily.ps1') -Distribution Ubuntu -LinuxUser ljjw -ProjectPath /home/ljjw/projects/ipo-monitor -Probe
