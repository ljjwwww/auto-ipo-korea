#Requires -Version 5.1
$ErrorActionPreference='Stop'
$t=Get-ScheduledTask -TaskName AutoIpoKorea-Daily -TaskPath '\'
$a=$t.Actions[0]
$base=Join-Path $env:TEMP ('auto-ipo-probe-'+[guid]::NewGuid().ToString('N'))
$arguments=$a.Arguments.Replace('"daily"','"--help"')
$p=Start-Process -FilePath $a.Execute -ArgumentList $arguments -PassThru -Wait -WindowStyle Hidden -RedirectStandardOutput ($base+'.out') -RedirectStandardError ($base+'.err')
[pscustomobject]@{Probe='exact-action-help-only';ExitCode=$p.ExitCode;Stdout=(Get-Content ($base+'.out') -Raw);Stderr=(Get-Content ($base+'.err') -Raw)} | ConvertTo-Json -Compress
$fixedArguments=$arguments -replace '"([^"\s]+)"','$1'
$p=Start-Process -FilePath $a.Execute -ArgumentList $fixedArguments -PassThru -Wait -WindowStyle Hidden -RedirectStandardOutput ($base+'.fixed.out') -RedirectStandardError ($base+'.fixed.err')
[pscustomobject]@{Probe='fixed-flags-help-only';ExitCode=$p.ExitCode;Stdout=[string]::Join("`n",@(Get-Content ($base+'.fixed.out')));Stderr=[string]::Join("`n",@(Get-Content ($base+'.fixed.err')))} | ConvertTo-Json -Compress
