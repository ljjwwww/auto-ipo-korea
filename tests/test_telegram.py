from contextlib import redirect_stdout, redirect_stderr
import io
from datetime import date
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import httpx

from ipo_monitor import CollectionError, main
from ipo_monitor.telegram import (TelegramError, render_digest, request_api,
                                  send_daily, split_messages)

TOKEN = '123456789:' + 'x' * 35
CREDS = {'token': TOKEN, 'chat_id': '1234567'}


class TelegramTests(unittest.TestCase):
    def setUp(self):
        today_patch=patch('ipo_monitor.telegram._today',return_value=date(2026,11,16))
        today_patch.start();self.addCleanup(today_patch.stop)
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root/'data.sqlite3'; self.state = self.root/'telegram.sqlite3'
        shutil.copytree('samples/assessment', self.root/'sample')
        self.sample = self.root/'sample'; self.aliases = self.sample/'stocks.toml'
        self.policy = self.root/'policy.toml'; shutil.copy('config/decision.toml', self.policy)
        self.cli('collect', '--offline', '--config', str(self.sample/'sources.toml'), '--db', str(self.db))
        self.cli('assess', '--db', str(self.db), '--fixture', str(self.sample/'responses.json'),
                 '--aliases', str(self.aliases), '--policy', str(self.policy))

    def cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = main(list(args))
        self.assertEqual(code, 0, out.getvalue())

    def mark_ollama(self):
        with sqlite3.connect(self.db) as conn:
            for row_id, value in conn.execute('SELECT id,result_json FROM decision_versions').fetchall():
                result = json.loads(value); result['analysis_origin'] = 'ollama'
                conn.execute('UPDATE decision_versions SET result_json=? WHERE id=?',
                             (json.dumps(result, ensure_ascii=False), row_id))

    def send(self, **kwargs):
        return send_daily(self.db, self.aliases, self.policy, self.state,
                          credential=CREDS, allow_send=True, **kwargs)

    def receipts(self):
        with sqlite3.connect(self.state) as conn:
            return conn.execute('SELECT state,attempts,error_code FROM telegram_chunks ORDER BY position').fetchall()

    def test_fixture_excluded_and_dryrun_no_write_or_network_or_credentials(self):
        with patch('httpx.post', side_effect=AssertionError('network')):
            rendered = render_digest(self.db, self.aliases, self.policy)
            self.assertEqual(rendered['skipped_fixture'], 3)
            self.assertFalse(rendered['chunks'])
            self.mark_ollama()
            summary = send_daily(self.db, self.aliases, self.policy, self.state, dry_run=True)
        self.assertEqual(summary['status'], 'preview')
        self.assertEqual(summary['stocks'], 3)
        text = '\n'.join(summary['messages'])
        self.assertIn('⭐⭐', text); self.assertIn('❌', text); self.assertIn('판단 불가', text)
        self.assertIn('https://', text)
        self.assertFalse(self.state.exists()); self.assertFalse(self.state.with_suffix('.lock').exists())

    def test_subscription_end_day_included_next_korean_day_excluded(self):
        self.mark_ollama()
        with patch('ipo_monitor.telegram._today',return_value=date(2026,11,20)):
            result=render_digest(self.db,self.aliases,self.policy)
            self.assertEqual(result['stocks'],3)
            self.assertEqual(result['skipped_expired'],0)
        before=self.db.read_bytes()
        with patch('ipo_monitor.telegram._today',return_value=date(2026,11,21)),patch('httpx.post',side_effect=AssertionError('send')):
            result=self.send()
            self.assertEqual(result['status'],'empty')
            self.assertEqual(result['stocks'],0)
            self.assertEqual(result['skipped_expired'],3)
        self.assertEqual(before,self.db.read_bytes())
        self.assertFalse(self.state.exists())

    def test_current_only_stale_source_and_policy_excluded(self):
        self.mark_ollama()
        self.assertEqual(render_digest(self.db, self.aliases, self.policy)['stocks'], 3)
        self.policy.write_text(self.policy.read_text().replace('candidate = 0.4', 'candidate = 0.8'))
        result = render_digest(self.db, self.aliases, self.policy)
        self.assertEqual(result['stocks'], 0); self.assertEqual(result['skipped_stale'], 3)
        shutil.copy('config/decision.toml', self.policy)
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE source_snapshots SET payload_json=replace(payload_json,'참여','참여 수정') WHERE source_id LIKE '%pang%'")
        result = render_digest(self.db, self.aliases, self.policy)
        self.assertLess(result['stocks'], 3)

    def test_confirmed_send_and_repeat_deduplicated_without_changing_previews(self):
        self.mark_ollama()
        with patch('ipo_monitor.telegram.request_api', return_value={'message_id': 42}) as request:
            first = self.send(); calls = request.call_count
            self.assertGreater(calls, 0); self.assertEqual(first['status'], 'sent')
            second = self.send()
            self.assertEqual(second['status'], 'unchanged'); self.assertEqual(second['sent'], 0)
            self.assertEqual(request.call_count, calls)
            payload = request.call_args.args[2]
            self.assertNotIn('parse_mode', payload)
            self.assertEqual(payload['link_preview_options'], {'is_disabled': True})
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT DISTINCT status FROM notification_outbox').fetchall(), [('preview',)])
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)

    def test_same_content_new_transition_is_a_new_delivery(self):
        self.mark_ollama()
        with patch('ipo_monitor.telegram.request_api', return_value={'message_id': 42}) as request:
            self.send(); before = request.call_count
            with sqlite3.connect(self.db) as conn:
                stock, version = conn.execute('SELECT stock,version_id FROM decision_current LIMIT 1').fetchone()
                transition = conn.execute("INSERT INTO decision_transitions(stock,version_id,selected_at) VALUES (?,?,?)", (stock, version, 'later')).lastrowid
                conn.execute('UPDATE decision_transition_current SET transition_id=? WHERE stock=?', (transition, stock))
            self.assertEqual(self.send()['status'], 'sent')
            self.assertGreater(request.call_count, before)

    def test_timeout_unknown_never_automatically_resent(self):
        self.mark_ollama()
        with patch('ipo_monitor.telegram.request_api', side_effect=TelegramError('telegram_network_error', uncertain=True)) as request:
            first = self.send(); self.assertEqual(first['status'], 'unknown')
            before = request.call_count; self.send()
            self.assertEqual(request.call_count, before)
        self.assertEqual(self.receipts()[0], ('unknown', 1, 'telegram_network_error'))

    def test_multiple_chunks_are_paced_after_first_dispatched_chunk(self):
        rendered = {'stocks': 1, 'transitions': [1], 'identity': 'mock-digest',
                    'chunks': ['part1', 'part2', 'part3'], 'skipped_stale': 0, 'skipped_fixture': 0}
        events = []
        def request(*args):
            events.append('send')
            return {'message_id': len(events)}
        with patch('ipo_monitor.telegram.render_digest', return_value=rendered), patch(
                'ipo_monitor.telegram.request_api', side_effect=request), patch(
                'ipo_monitor.telegram.time.sleep', side_effect=lambda seconds: events.append(seconds)) as sleep:
            self.assertEqual(self.send()['status'], 'sent')
            self.assertEqual(events, ['send', 1.05, 'send', 1.05, 'send'])
            self.assertEqual(sleep.call_count, 2)
            events.clear()
            self.assertEqual(self.send()['status'], 'unchanged')
            self.assertEqual(events, [])

    def test_unknown_chunk_blocks_later_pending_parts_on_next_run(self):
        rendered = {'stocks': 1, 'transitions': [1], 'identity': 'mock-digest',
                    'chunks': ['part1', 'part2'], 'skipped_stale': 0, 'skipped_fixture': 0}
        with patch('ipo_monitor.telegram.render_digest', return_value=rendered), patch(
                'ipo_monitor.telegram.request_api', side_effect=TelegramError('telegram_network_error', uncertain=True)) as request:
            self.assertEqual(self.send()['status'], 'unknown')
            self.assertEqual(self.send()['status'], 'unknown')
            self.assertEqual(request.call_count, 1)
        self.assertEqual([row[0] for row in self.receipts()], ['unknown', 'pending'])

    def test_known_rejection_retry_only_next_run_and_stop_after_three(self):
        self.mark_ollama()
        with patch('ipo_monitor.telegram.request_api', side_effect=TelegramError('telegram_http_403')) as request:
            for i in range(5):
                result = self.send(); self.assertEqual(result['status'], 'failed')
            self.assertEqual(request.call_count, 3)
        self.assertEqual(self.receipts()[0], ('failed', 3, 'telegram_http_403'))

    def test_exhausted_first_chunk_blocks_unsent_tail(self):
        rendered = {'stocks': 1, 'transitions': [1], 'identity': 'mock-digest',
                    'chunks': ['part1', 'part2'], 'skipped_stale': 0, 'skipped_fixture': 0}
        with patch('ipo_monitor.telegram.render_digest', return_value=rendered), patch(
                'ipo_monitor.telegram.request_api', side_effect=TelegramError('telegram_http_403')) as request:
            for _ in range(4):
                self.assertEqual(self.send()['status'], 'failed')
            self.assertEqual(request.call_count, 3)
        self.assertEqual([row[:2] for row in self.receipts()], [('failed', 3), ('pending', 0)])

    def test_crash_sending_converted_unknown_before_retry(self):
        self.mark_ollama()
        with patch('ipo_monitor.telegram.request_api', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): self.send()
        self.assertEqual(self.receipts()[0][0], 'sending')
        with patch('ipo_monitor.telegram.request_api', side_effect=AssertionError('duplicate')):
            self.assertEqual(self.send()['status'], 'unknown')
        self.assertEqual(self.receipts()[0][0], 'unknown')

    def test_opt_in_private_credentials_and_state_symlink_required(self):
        self.mark_ollama()
        with self.assertRaises(CollectionError):
            send_daily(self.db, self.aliases, self.policy, self.state, credential=CREDS)
        for value in ({'token': TOKEN, 'chat_id': '-100123'}, {'token': TOKEN+'\n', 'chat_id': '1'}):
            with self.assertRaises(CollectionError):
                send_daily(self.db, self.aliases, self.policy, self.state, credential=value, allow_send=True)
        target = self.root/'target'; target.write_text('not a database'); self.state.symlink_to(target)
        with self.assertRaises(CollectionError): self.send()
        self.assertEqual(target.read_text(), 'not a database')

    def test_unicode_chunk_limit_and_lossless_split(self):
        text = '가나다⭐😀' * 4000
        chunks = split_messages(text)
        self.assertEqual(''.join(chunks), text)
        self.assertTrue(all(len(c.encode('utf-16-le'))//2 <= 3500 for c in chunks))

    def test_raw_stars_visible_for_unprocessed_site(self):
        self.mark_ollama()
        with sqlite3.connect(self.db) as conn:
            for row_id, value in conn.execute('SELECT id,result_json FROM decision_versions').fetchall():
                result = json.loads(value)
                for signal in result['contributions']:
                    if signal['source_type'] == 'ipo_site': signal['analysis_status'] = 'unprocessed'
                conn.execute('UPDATE decision_versions SET result_json=? WHERE id=?', (json.dumps(result), row_id))
        text = '\n'.join(render_digest(self.db, self.aliases, self.policy)['chunks'])
        self.assertIn('분석 대기 / 표시 ⭐⭐', text)


class TelegramTransportTests(unittest.TestCase):
    def response(self, status, value):
        return httpx.Response(status, json=value)

    def test_fixed_host_no_proxy_no_redirect_no_markup(self):
        with patch('httpx.post', return_value=self.response(200, {'ok': True, 'result': {'message_id': 7}})) as post:
            self.assertEqual(request_api(TOKEN, 'sendMessage', {'text': 'test'})['message_id'], 7)
            args, kwargs = post.call_args
            self.assertEqual(args[0], 'https://api.telegram.org/bot'+TOKEN+'/sendMessage')
            self.assertFalse(kwargs['follow_redirects']); self.assertFalse(kwargs['trust_env'])
            self.assertEqual(kwargs['timeout'], 20)

    def test_sanitized_transport_failures_and_uncertain_results(self):
        examples = [(httpx.TimeoutException('token='+TOKEN), True, 'network'),
                    (self.response(502, {}), True, 'server'),
                    (self.response(302, {}), True, 'redirect'),
                    (self.response(200, {'ok': True, 'result': {}}), True, 'response'),
                    (self.response(403, {'description': TOKEN}), False, '403'),
                    (self.response(200, {'ok': False, 'error_code': 429, 'description': TOKEN}), False, '429')]
        for response, uncertain, code in examples:
            with self.subTest(code=code):
                kwargs = {'side_effect': response} if isinstance(response, Exception) else {'return_value': response}
                with patch('httpx.post', **kwargs), self.assertRaises(TelegramError) as caught:
                    request_api(TOKEN, 'sendMessage', {})
                self.assertEqual(caught.exception.uncertain, uncertain)
                self.assertIn(code, str(caught.exception)); self.assertNotIn(TOKEN, str(caught.exception))

    def test_onboarding_methods_and_unsupported_method_blocked(self):
        with patch('httpx.post', return_value=self.response(200, {'ok': True, 'result': [{'update_id': 1}]})):
            self.assertEqual(request_api(TOKEN, 'getUpdates', {}), [{'update_id': 1}])
        with patch('httpx.post', side_effect=AssertionError('network')):
            for method, token in [('deleteWebhook', TOKEN), ('getMe', 'bad')]:
                with self.assertRaises(TelegramError): request_api(token, method, {})


class SubscriptionExpiryTests(unittest.TestCase):
    def entries(self,events,kind='ipo_site'):
        return [{'input':{'source_type':kind,'content':{'events':events}}}]

    def ended(self,events,today=date(2026,11,12),kind='ipo_site'):
        from ipo_monitor.telegram import _subscription_ended
        return _subscription_ended(self.entries(events,kind),today)

    def test_max_subscription_date_not_first_day_or_listing_date(self):
        events=[{'kind':'subscription','date':'2026-11-10'},
                {'kind':'subscription','date':'2026-11-11'},
                {'kind':'listing','date':'2026-11-20'}]
        self.assertFalse(self.ended(events,date(2026,11,11)))
        self.assertTrue(self.ended(events,date(2026,11,12)))
        self.assertFalse(self.ended(events,date(2026,11,9)))

    def test_missing_invalid_and_listing_only_dates_are_retained(self):
        for events in [[],[{'kind':'listing','date':'2026-11-01'}],
                       [{'kind':'subscription'}],
                       [{'kind':'subscription','date':'2026-02-30'}],
                       [{'kind':'subscription','date':'20261101'}],
                       [{'kind':'subscription','date':'2026-11-01'},{'kind':'subscription','date':None}]]:
            with self.subTest(events=events):self.assertFalse(self.ended(events))

    def test_latest_extension_kept_and_non_site_dates_not_inferred(self):
        events=[{'kind':'subscription','date':'2026-11-01'},
                {'kind':'subscription','date':'2026-11-13'}]
        self.assertFalse(self.ended(events))
        self.assertFalse(self.ended([{'kind':'subscription','date':'2026-11-01'}],kind='rss'))

    def test_today_uses_korean_timezone_at_utc_day_boundary(self):
        from ipo_monitor.telegram import _today
        from datetime import datetime
        from zoneinfo import ZoneInfo
        with patch('ipo_monitor.telegram.datetime') as clock:
            clock.now.return_value=datetime(2026,11,12,0,0,tzinfo=ZoneInfo('Asia/Seoul'))
            self.assertEqual(_today(),date(2026,11,12))
            self.assertEqual(clock.now.call_args.args[0].key,'Asia/Seoul')
