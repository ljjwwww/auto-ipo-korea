from contextlib import redirect_stdout,redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import io
import unittest

from ipo_monitor import main,CollectionError
from ipo_monitor.telegram_command import send_from_daily,load_config


class TelegramCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.config=self.root/'telegram.toml'
        self.config.write_text('[telegram]\nproject_root="."\nenabled = false\nstate_db="state.sqlite3"\n')
        self.daily={'telegram_config':self.config,'backend':'ollama','db':self.root/'db.sqlite3',
                    'aliases':self.root/'stocks.toml','policy':self.root/'policy.toml'}

    def call(self,action,*extra):
        out=io.StringIO()
        with redirect_stdout(out),redirect_stderr(out):
            code=main(['telegram',action,'--config',str(self.config),*extra])
        return code,out.getvalue()

    def test_disabled_daily_never_reads_credentials_or_transports(self):
        with patch('ipo_monitor.telegram_credentials.read_credentials',side_effect=AssertionError('secret')),patch('ipo_monitor.telegram.send_daily',side_effect=AssertionError('send')):
            self.assertEqual(send_from_daily(self.daily)['status'],'disabled')

    def test_enable_requires_credentials_disable_does_not(self):
        with patch('ipo_monitor.telegram_credentials.read_credentials',side_effect=CollectionError('missing')):
            code,out=self.call('enable');self.assertEqual(code,1,out)
            self.assertFalse(load_config(self.config)['enabled'])
        with patch('ipo_monitor.telegram_credentials.read_credentials',return_value={'token':'not_printed','chat_id':'123'}):
            code,out=self.call('enable');self.assertEqual(code,0,out)
            self.assertNotIn('not_printed',out)
            self.assertTrue(load_config(self.config)['enabled'])
        with patch('ipo_monitor.telegram_credentials.read_credentials',side_effect=AssertionError('secret')):
            code,out=self.call('disable');self.assertEqual(code,0,out)
            self.assertFalse(load_config(self.config)['enabled'])

    def test_manual_send_requires_both_flag_and_enabled_configuration(self):
        with patch('ipo_monitor.daily.load_config',return_value=self.daily),patch('ipo_monitor.telegram_credentials.read_credentials',side_effect=AssertionError('secret')),patch('ipo_monitor.telegram.send_daily',side_effect=AssertionError('send')):
            self.assertEqual(self.call('send')[0],1)
            self.assertEqual(self.call('send','--allow-send')[0],1)

    def test_enabled_daily_explicitly_passes_send_optin_and_never_for_fixture(self):
        self.config.write_text(self.config.read_text().replace('false','true'))
        credential={'token':'not_printed','chat_id':'123'}
        with patch('ipo_monitor.telegram_credentials.read_credentials',return_value=credential),patch('ipo_monitor.telegram.send_daily',return_value={'status':'sent','sent':1}) as send:
            self.assertEqual(send_from_daily(self.daily)['sent'],1)
            send.assert_called_once_with(self.daily['db'],self.daily['aliases'],self.daily['policy'],self.root/'state.sqlite3',credential,allow_send=True)
        self.daily['backend']='fixture'
        with patch('ipo_monitor.telegram_credentials.read_credentials',side_effect=AssertionError('secret')):
            self.assertEqual(send_from_daily(self.daily)['status'],'fixture_blocked')

    def test_preview_is_credential_and_network_free(self):
        with patch('ipo_monitor.daily.load_config',return_value=self.daily),patch('ipo_monitor.telegram_credentials.read_credentials',side_effect=AssertionError('secret')),patch('ipo_monitor.telegram.render_digest',return_value={'chunks':['example']}),patch('httpx.post',side_effect=AssertionError('HTTP')):
            code,out=self.call('preview');self.assertEqual(code,0,out)
            self.assertIn('example',out)

    def test_manual_send_passes_optin_no_extra_ai(self):
        self.config.write_text(self.config.read_text().replace('false','true'))
        with patch('ipo_monitor.daily.load_config',return_value=self.daily),patch('ipo_monitor.telegram_credentials.read_credentials',return_value={'token':'not_printed','chat_id':'123'}),patch('ipo_monitor.telegram.send_daily',return_value={'status':'unchanged','sent':0}) as send:
            code,out=self.call('send','--allow-send');self.assertEqual(code,0,out)
            self.assertTrue(send.call_args.kwargs['allow_send'])
            self.assertNotIn('not_printed',out)
