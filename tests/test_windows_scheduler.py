"""Static regression contracts; Windows runtime probes are separate evidence."""
from pathlib import Path
import unittest


class WindowsSchedulerTests(unittest.TestCase):
    def text(self,name):return (Path('scripts/windows')/name).read_text()

    def test_native_flags_are_not_unconditionally_quoted(self):
        for name in ['register-daily.ps1','remove-daily.ps1','run-daily.ps1']:
            text=self.text(name)
            self.assertIn("if ($_ -match '\\s')",text)
        runner=self.text('run-daily.ps1')
        self.assertIn('$arguments = Join-NativeArguments $tokens',runner)
        self.assertIn("@('-d', $Distribution",runner)
        self.assertNotIn("$tokens | ForEach-Object { '\"' + $_ + '\"' }",runner)

    def test_launcher_logs_before_process_and_redirects_both_streams(self):
        text=self.text('run-daily.ps1')
        self.assertLess(text.index("($prefix+'.run.json')"),text.index('$process.Start()'))
        for token in ['RedirectStandardOutput = $true','RedirectStandardError = $true',
                      'ReadToEndAsync()','WaitForExit()','StandardOutputEncoding = [System.Text.Encoding]::UTF8',
                      'StandardErrorEncoding = [System.Text.Encoding]::UTF8','exit $exitCode']:
            self.assertIn(token,text)
        self.assertNotIn('$_.Exception.Message',text)

    def test_probe_only_uses_help_and_registration_never_launches_task(self):
        self.assertIn("if ($Probe) { $tokens += '--help' } else",self.text('run-daily.ps1'))
        for name in ['register-daily.ps1','remove-daily.ps1','run-daily.ps1']:
            text=self.text(name)
            self.assertNotIn('Start-ScheduledTask',text)
            self.assertNotIn('schtasks /run',text.lower())
        register=self.text('register-daily.ps1')
        self.assertIn('(Get-Date).AddMinutes($TestAfterMinutes)',register)
        self.assertIn('New-ScheduledTaskTrigger -Once -At $testAt',register)
        self.assertLess(register.index('Copy-Item -LiteralPath $sourceRunner'),register.index('$testAt ='))
        self.assertIn("New-ScheduledTaskTrigger -Daily -At $time",register)

    def test_legacy_migration_and_running_task_protection(self):
        for name in ['register-daily.ps1','remove-daily.ps1']:
            text=self.text(name)
            for token in ['$legacyArguments','$legacyMarker','$current',"$task.State -eq 'Running'",
                          "[int]$Principal.LogonType -eq 3","[int]$Principal.RunLevel -eq 0"]:
                self.assertIn(token,text)
        self.assertLess(self.text('register-daily.ps1').index("$task.State -eq 'Running'"),
                        self.text('register-daily.ps1').index('Copy-Item'))

    def test_owner_uses_sid_resolution_not_name_spelling(self):
        for name in ['register-daily.ps1','remove-daily.ps1']:
            text=self.text(name)
            self.assertIn('NTAccount($account)',text)
            self.assertIn('.Translate([Security.Principal.SecurityIdentifier])',text)
            self.assertIn('$sid.Value -eq $Identity.User.Value',text)
            self.assertNotIn('$task.Principal.UserId -notin',text)

    def test_fixed_time_test_only_returns_before_touching_daily_task(self):
        text=self.text('register-daily.ps1')
        start=text.index('if ($TestOnly)')
        end=text.index("$sourceRunner =",start)
        branch=text[start:end]
        self.assertIn('Export-ScheduledTask',branch)
        self.assertIn('$dailyBefore -cne $dailyAfter',branch)
        self.assertIn('New-ScheduledTaskTrigger -Once -At $TestAt',branch)
        self.assertIn('.EndBoundary =',branch)
        self.assertIn("$TestAt.Date -ne [datetime]::Today",branch)
        self.assertIn('return',branch)
        self.assertNotIn('Copy-Item',branch)
        self.assertNotIn('Register-ScheduledTask -TaskName $TaskName',branch)
