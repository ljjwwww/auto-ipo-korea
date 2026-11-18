import io
import os
from contextlib import redirect_stdout,redirect_stderr
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ipo_monitor import CollectionError
from ipo_monitor.credentials import save_key,read_key,command
from types import SimpleNamespace


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.cwd=patch('ipo_monitor.credentials.Path.cwd',return_value=self.root);self.cwd.start();self.addCleanup(self.cwd.stop)
        self.env=patch.dict(os.environ,{},clear=True);self.env.start();self.addCleanup(self.env.stop)
        self.key='test-only-ollama-key-not-a-real-credential'
    def test_store_private_permissions_and_round_trip(self):
        save_key(self.key)
        self.assertEqual(read_key(),self.key)
        p=self.root/'data/secrets/ollama.key'
        self.assertEqual(p.stat().st_mode&0o777,0o600)
        self.assertEqual(p.parent.stat().st_mode&0o777,0o700)
    def test_environment_override_and_no_unsafe_chars(self):
        save_key(self.key)
        with patch.dict(os.environ,{'OLLAMA_API_KEY':'environment-test-key'}):self.assertEqual(read_key(),'environment-test-key')
        for value in ('','has space','newline\nkey','x'*4097):
            with self.assertRaises(CollectionError):save_key(value)
    def test_refuse_symlink_parent_or_key(self):
        outside=self.root/'elsewhere';outside.mkdir()
        (self.root/'data').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(CollectionError):save_key(self.key)
        (self.root/'data').unlink();save_key(self.key)
        p=self.root/'data/secrets/ollama.key';p.unlink();p.symlink_to(outside/'key')
        with self.assertRaises(CollectionError):save_key(self.key)
        with self.assertRaises(CollectionError):read_key()
    def test_refuse_public_file_permissions(self):
        save_key(self.key);(self.root/'data/secrets/ollama.key').chmod(0o644)
        with self.assertRaises(CollectionError):read_key()
    def test_noninteractive_input_does_not_read_or_echo(self):
        with patch('sys.stdin.isatty',return_value=False),patch('getpass.getpass') as prompt,redirect_stderr(io.StringIO()):
            self.assertEqual(command(SimpleNamespace(action='set')),1);prompt.assert_not_called()
    def test_prompt_confirmation_and_status_redact_secret(self):
        out=io.StringIO()
        with patch('sys.stdin.isatty',return_value=True),patch('getpass.getpass',side_effect=[self.key,self.key]),redirect_stdout(out):
            self.assertEqual(command(SimpleNamespace(action='set')),0)
            self.assertEqual(command(SimpleNamespace(action='status')),0)
        self.assertNotIn(self.key,out.getvalue())
    def test_mismatch_preserves_existing_file(self):
        save_key(self.key)
        with patch('sys.stdin.isatty',return_value=True),patch('getpass.getpass',side_effect=['different','mismatch']),redirect_stderr(io.StringIO()):
            self.assertEqual(command(SimpleNamespace(action='set')),1)
        self.assertEqual(read_key(),self.key)

    def test_getpass_fallback_warning_or_cancel_keeps_existing_secret(self):
        import getpass
        save_key(self.key)
        for error in (getpass.GetPassWarning('unsafe fallback'),EOFError(),KeyboardInterrupt()):
            out=io.StringIO()
            with patch('sys.stdin.isatty',return_value=True),patch('getpass.getpass',side_effect=error),redirect_stderr(out):
                self.assertEqual(command(SimpleNamespace(action='set')),1)
            self.assertEqual(read_key(),self.key)
            self.assertNotIn(self.key,out.getvalue())
