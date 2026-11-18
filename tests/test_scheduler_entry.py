from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from ipo_monitor.scheduler_entry import main,prepare_path


class SchedulerEntryTests(unittest.TestCase):
    def test_minimal_scheduler_path_discovers_user_tool_and_keeps_system_path(self):
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory);tools=home/'.local/bin';tools.mkdir(parents=True)
            executable=tools/'uvx';executable.write_text('#!/bin/sh\nexit 0\n');executable.chmod(0o700)
            value=prepare_path('/usr/bin:/bin',home,'/project/.venv/bin/python')
            import shutil
            self.assertEqual(shutil.which('uvx',path=value),str(executable))
            self.assertIn('/usr/bin',value.split(os.pathsep))
            self.assertEqual(prepare_path(value,home,'/project/.venv/bin/python'),value)

    def test_entry_restores_local_tools_before_cli_without_changing_arguments(self):
        with patch.dict(os.environ,{'PATH':'/usr/bin:/bin'}),patch('ipo_monitor.scheduler_entry.Path.home',return_value=Path('/home/example')),patch('ipo_monitor.main',return_value=0) as cli:
            self.assertEqual(main(['--help']),0)
            cli.assert_called_once_with(['--help'])
            self.assertIn('/home/example/.local/bin',os.environ['PATH'].split(os.pathsep))
