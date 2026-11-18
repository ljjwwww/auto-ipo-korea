from contextlib import nullcontext, redirect_stderr, redirect_stdout
import getpass
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from ipo_monitor import CollectionError
from ipo_monitor.telegram_credentials import (
    _paired_chat, read_credentials, request_api, save_credentials, setup, status,
)

TOKEN = '123456789:abcdefghijklmnopqrstuvwxyz_123456789'
OTHER_TOKEN = '987654321:abcdefghijklmnopqrstuvwxyz_987654321'
PAIR = '/start IPO-test-pair-code'
BOT = {'id': 123456789, 'is_bot': True, 'username': 'test_ipo_bot'}


def update(chat_id=321, *, text=PAIR, kind='private', sender=None, is_bot=False):
    return {'update_id': 1, 'message': {'text': text,
            'chat': {'id': chat_id, 'type': kind},
            'from': {'id': chat_id if sender is None else sender, 'is_bot': is_bot}}}


class TelegramCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cwd = patch('ipo_monitor.telegram_credentials.Path.cwd', return_value=self.root)
        self.cwd.start(); self.addCleanup(self.cwd.stop)
        self.args = SimpleNamespace()
        self.path = self.root / 'data/secrets/telegram.json'

    def invoke_setup(self, responses=None, tokens=None, terminal=True):
        out = io.StringIO()
        with (patch('sys.stdin.isatty', return_value=terminal),
              patch('getpass.getpass', side_effect=tokens or [TOKEN, TOKEN]) as prompt,
              patch('builtins.input', return_value=''),
              patch('ipo_monitor.telegram_credentials.secrets.token_urlsafe', return_value='test-pair-code'),
              patch('ipo_monitor.telegram_credentials.request_api', side_effect=responses or [BOT, [update()]]) as api,
              redirect_stdout(out), redirect_stderr(out)):
            code = setup(self.args)
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertNotIn(OTHER_TOKEN, out.getvalue())
        return code, out.getvalue(), prompt, api

    def test_roundtrip_private_permissions_no_plaintext_output(self):
        save_credentials(TOKEN, '321')
        self.assertEqual(read_credentials(), {'token': TOKEN, 'chat_id': '321'})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        out = io.StringIO()
        with redirect_stdout(out), patch('httpx.stream', side_effect=AssertionError('no API')):
            self.assertEqual(status(self.args), 0)
        self.assertNotIn(TOKEN, out.getvalue()); self.assertNotIn('321', out.getvalue())

    def test_read_rejects_public_file_and_directory_permissions(self):
        save_credentials(TOKEN, '321'); self.path.chmod(0o644)
        with self.assertRaises(CollectionError): read_credentials()
        self.path.chmod(0o600); self.path.parent.chmod(0o755)
        with self.assertRaises(CollectionError): read_credentials()

    def test_refuse_symlink_parent_and_file(self):
        outside = self.root / 'outside'; outside.mkdir()
        (self.root / 'data').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(CollectionError): save_credentials(TOKEN, '321')
        with self.assertRaises(CollectionError): read_credentials()
        (self.root / 'data').unlink(); save_credentials(TOKEN, '321')
        self.path.unlink(); self.path.symlink_to(outside / 'secret')
        with self.assertRaises(CollectionError): save_credentials(TOKEN, '321')
        with self.assertRaises(CollectionError): read_credentials()

    def test_reject_nonregular_malformed_and_oversized_files(self):
        save_credentials(TOKEN, '321')
        for text in ['not json', '[]', '{"token":"x","chat_id":"321"}', 'x' * 4097,
                     json.dumps({'token': TOKEN, 'chat_id': '321', 'extra': 'ignored?'})]:
            self.path.write_text(text)
            with self.assertRaises(CollectionError): read_credentials()
        self.path.unlink(); os.mkfifo(self.path, 0o600)
        with self.assertRaises(CollectionError): read_credentials()

    def test_reject_invalid_token_chat_and_wrong_owner(self):
        for token in ['', 'space key', '1:short', TOKEN + '\n', TOKEN + '/sendMessage']:
            with self.assertRaises(CollectionError): save_credentials(token, '321')
        for chat in ['-321', '0', '321\n', 321, 'group', '3' * 20]:
            with self.assertRaises(CollectionError): save_credentials(TOKEN, chat)
        save_credentials(TOKEN, '321')
        with patch('ipo_monitor.telegram_credentials.os.getuid', return_value=os.getuid() + 1):
            with self.assertRaises(CollectionError): read_credentials()

    def test_setup_pairs_one_time_private_command_without_sending(self):
        code, out, prompt, api = self.invoke_setup()
        self.assertEqual(code, 0, out); self.assertEqual(prompt.call_count, 2)
        self.assertEqual(read_credentials()['chat_id'], '321')
        self.assertEqual([c.args[1] for c in api.call_args_list], ['getMe', 'getUpdates'])
        self.assertEqual(api.call_args_list[1].args[2], {'limit': 100, 'timeout': 0, 'allowed_updates': ['message']})
        self.assertIn(PAIR, out)

    def test_setup_noninteractive_or_mismatch_has_no_network_or_overwrite(self):
        save_credentials(TOKEN, '321')
        code, out, prompt, api = self.invoke_setup(terminal=False)
        self.assertEqual(code, 1, out); prompt.assert_not_called(); api.assert_not_called()
        code, out, _, api = self.invoke_setup(tokens=[TOKEN, OTHER_TOKEN])
        self.assertEqual(code, 1, out); api.assert_not_called()
        self.assertEqual(read_credentials()['token'], TOKEN)

    def test_setup_pairing_missing_ambiguous_and_invalid_bot_keep_old_file(self):
        save_credentials(TOKEN, '321')
        for responses in [[BOT, [update(text='/start')]],
                          [BOT, [update(321), update(654)]],
                          [{'id': 1, 'is_bot': False, 'username': 'not_a_bot'}],
                          [BOT, [update(-42, kind='supergroup')]]]:
            code, out, _, _ = self.invoke_setup(responses=responses, tokens=[OTHER_TOKEN, OTHER_TOKEN])
            self.assertEqual(code, 1, out)
            self.assertEqual(read_credentials(), {'token': TOKEN, 'chat_id': '321'})

    def test_pairing_excludes_forged_sender_bots_groups_and_old_start(self):
        for invalid in [update(text='/start'), update(kind='group'), update(sender=999),
                        update(is_bot=True), update(chat_id=True), update(text=['malformed']), {}, 'bad']:
            with self.assertRaises(CollectionError): _paired_chat([invalid], PAIR, BOT['username'])
        self.assertEqual(_paired_chat([update(text='/start'), update(), update()], PAIR, BOT['username']), '321')
        tagged = PAIR.replace('/start ', '/start@test_ipo_bot ')
        self.assertEqual(_paired_chat([update(text=tagged)], PAIR, BOT['username']), '321')

    def test_fallback_warning_eof_keyboardinterrupt_do_not_write(self):
        for error in [getpass.GetPassWarning('unsafe echo'), EOFError(), KeyboardInterrupt()]:
            out = io.StringIO()
            with (patch('sys.stdin.isatty', return_value=True),
                  patch('getpass.getpass', side_effect=error),
                  patch('ipo_monitor.telegram_credentials.request_api') as api,
                  redirect_stderr(out)):
                self.assertEqual(setup(self.args), 1)
                api.assert_not_called()
            self.assertFalse(self.path.exists())

    def test_config_project_root_and_status_missing_do_not_touch_network(self):
        directory = self.root / 'config'; directory.mkdir()
        config = directory / 'telegram.toml'
        config.write_text('[telegram]\nproject_root=".."\nenabled=false\n')
        save_credentials(TOKEN, '321')
        with redirect_stdout(io.StringIO()), patch('httpx.stream', side_effect=AssertionError('network')):
            self.assertEqual(status(SimpleNamespace(config=config)), 0)
            self.assertEqual(status(SimpleNamespace(config=self.root / 'missing.toml')), 1)

    def test_request_api_bounded_fixed_endpoint_no_redirect_or_environment(self):
        response = httpx.Response(200, json={'ok': True, 'result': BOT})
        with patch('httpx.stream', return_value=nullcontext(response)) as stream:
            self.assertEqual(request_api(TOKEN, 'getMe', {}), BOT)
        self.assertEqual(stream.call_args.args, ('POST', f'https://api.telegram.org/bot{TOKEN}/getMe'))
        self.assertEqual(stream.call_args.kwargs, {'json': {}, 'timeout': 20, 'follow_redirects': False, 'trust_env': False})
        with patch('httpx.stream') as stream:
            with self.assertRaises(CollectionError): request_api(TOKEN, 'sendMessage', {})
            stream.assert_not_called()

    def test_request_api_errors_never_expose_token_url_or_response_body(self):
        cases = [httpx.Response(302, headers={'location': 'https://example.com'}),
                 httpx.Response(401, text=TOKEN), httpx.Response(409, text=TOKEN),
                 httpx.Response(500, text=TOKEN), httpx.Response(200, text=TOKEN),
                 httpx.Response(200, json={'ok': False, 'description': TOKEN}),
                 httpx.Response(200, content=b' ' * (1024 * 1024 + 1))]
        for response in cases:
            with patch('httpx.stream', return_value=nullcontext(response)):
                with self.assertRaises(CollectionError) as raised: request_api(TOKEN, 'getMe', {})
                self.assertNotIn(TOKEN, str(raised.exception))
        error = httpx.ConnectError(f'failed https://api.telegram.org/bot{TOKEN}/getMe')
        with patch('httpx.stream', side_effect=error):
            with self.assertRaises(CollectionError) as raised: request_api(TOKEN, 'getMe', {})
            self.assertEqual(str(raised.exception), 'telegram_setup_network_error')


if __name__ == '__main__':
    unittest.main()
