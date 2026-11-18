"""Mock-only delivery tests; never load actual credentials or send messages."""
from datetime import date
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ipo_monitor import CollectionError
from ipo_monitor.mention_delivery import deliver_events
from ipo_monitor.telegram import TelegramError

TOKEN = '123456789:' + 'x' * 35
CREDS = {'token': TOKEN, 'chat_id': '1234567'}
TODAY = date(2026, 11, 17)


class MentionDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'events.sqlite3'
        with sqlite3.connect(self.path) as conn:
            conn.executescript('''CREATE TABLE mention_settings (key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE mention_catalog (month TEXT,stock TEXT,payload_json TEXT,PRIMARY KEY(month,stock));
                CREATE TABLE mention_events (id INTEGER PRIMARY KEY,event_hash TEXT UNIQUE,stock TEXT,
                source_id TEXT,source_type TEXT,item_id TEXT,snapshot_id INTEGER,input_hash TEXT,input_json TEXT,
                created_at TEXT,analysis_json TEXT,analysis_status TEXT,error_code TEXT,attempts INTEGER,
                analysis_origin TEXT DEFAULT 'unknown');''')
            conn.execute('INSERT INTO mention_settings VALUES (?,?)', ('catalog_date', TODAY.isoformat()))
        self.catalog()
        self.add_event()

    def catalog(self, stock='가상기업', month='2026-11', start='2026-11-18', end='2026-11-19'):
        with sqlite3.connect(self.path) as conn:
            conn.execute('INSERT OR REPLACE INTO mention_catalog VALUES (?,?,?)',
                         (month,stock,json.dumps({'stock':stock,'start':start,'end':end})))

    def add_event(self, stock='가상기업', kind='rss', status='success', origin='ollama', reason='청약에 참여한다고 밝혔습니다.'):
        with sqlite3.connect(self.path) as conn:
            number=conn.execute('SELECT COALESCE(max(id),0)+1 FROM mention_events').fetchone()[0]
            source={'stock':stock,'source_id':f'source-{kind}','source_type':kind,'creator':'팽' if kind=='rss' else '기릿',
                    'url':f'https://blog.naver.com/hycj1025/{123456789+number}' if kind=='rss' else 'https://www.youtube.com/watch?v=7uhTGAHdoCI',
                    'content':{'text':f'{stock} 청약에 참여하겠습니다.'}}
            result={'opinion':'참여','assessment':'positive','evidence':source['content']['text'],
                    'stage':'planned','reason':reason}
            conn.execute('''INSERT INTO mention_events
                (id,event_hash,stock,source_id,source_type,item_id,snapshot_id,input_hash,input_json,
                created_at,analysis_json,analysis_status,error_code,attempts,analysis_origin)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (number,f'event-{number}',stock,source['source_id'],kind,f'item-{number}',number,f'input-{number}',
                 json.dumps(source,ensure_ascii=False),'2026-11-17T10:00:00+09:00',json.dumps(result,ensure_ascii=False),
                 status,None,0,origin))
        return number

    def call(self, **kwargs):
        return deliver_events(self.path, **{'credential':CREDS,'allow_send':True,'today':TODAY,**kwargs})

    def receipts(self):
        with sqlite3.connect(self.path) as conn:
            return conn.execute('SELECT state,attempts,last_attempt_day FROM mention_delivery ORDER BY event_id').fetchall()

    def test_exact_event_line_source_and_no_allstock_digest(self):
        self.catalog(stock='아직언급안된기업')
        with patch('ipo_monitor.mention_delivery.request_api',return_value={'message_id':42}) as request:
            result=self.call()
        self.assertEqual(result['status'],'sent');self.assertEqual(result['sent'],1)
        payload=request.call_args.args[2]
        self.assertEqual(payload['chat_id'],CREDS['chat_id'])
        self.assertEqual(payload['text'],'[가상기업] 팽 블로그: 청약에 참여한다고 밝혔습니다. (의견: 참여)\nhttps://blog.naver.com/hycj1025/123456790')
        self.assertNotIn('아직언급안된기업',payload['text'])
        self.assertNotIn('parse_mode',payload)
        self.assertEqual(payload['link_preview_options'],{'is_disabled':True})
        self.assertNotIn(TOKEN,str(result));self.assertNotIn(CREDS['chat_id'],str(result))

    def test_repeat_has_zero_http_calls(self):
        with patch('ipo_monitor.mention_delivery.request_api',return_value={'message_id':42}) as request:
            self.assertEqual(self.call()['sent'],1)
            result=self.call()
            self.assertEqual(result['status'],'unchanged');self.assertEqual(result['sent'],0)
            self.assertEqual(request.call_count,1)
        self.assertEqual(self.receipts(),[('sent',1,'2026-11-17')])

    def test_confirmed_receipt_stays_unchanged_after_validation_policy_changes(self):
        with patch('ipo_monitor.mention_delivery.request_api',return_value={'message_id':42}):
            self.call()
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE mention_events SET analysis_json='{}'")
        with patch('ipo_monitor.mention_delivery.request_api',side_effect=AssertionError('resend')):
            result=self.call()
        self.assertEqual(result['status'],'unchanged')
        self.assertEqual(result['invalid'],0)

    def test_dry_run_never_writes_or_reads_credentials_or_network(self):
        before=self.path.read_bytes()
        with patch('ipo_monitor.mention_delivery.request_api',side_effect=AssertionError('network')):
            result=deliver_events(self.path,dry_run=True,today=TODAY)
        self.assertEqual(result['status'],'preview');self.assertEqual(len(result['messages']),1)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertFalse(self.path.with_suffix('.delivery.lock').exists())
        missing=self.path.parent/'absent.sqlite3'
        self.assertEqual(deliver_events(missing,dry_run=True,today=TODAY)['messages'],[])
        self.assertFalse(missing.exists())

    def test_dry_run_suppresses_confirmed_or_unknown_receipts_without_creds(self):
        with patch('ipo_monitor.mention_delivery.request_api',return_value={'message_id':42}):self.call()
        before=self.path.read_bytes()
        result=deliver_events(self.path,dry_run=True,today=TODAY)
        self.assertEqual(result['messages'],[]);self.assertEqual(result['unchanged'],1)
        self.assertEqual(self.path.read_bytes(),before)

    def test_timeout_unknown_never_auto_retries(self):
        with patch('ipo_monitor.mention_delivery.request_api',side_effect=TelegramError('telegram_network_error',uncertain=True)) as request:
            self.assertEqual(self.call()['status'],'unknown')
            self.assertEqual(self.call()['status'],'unknown')
            self.assertEqual(request.call_count,1)
        self.assertEqual(self.receipts()[0][:2],('unknown',1))

    def test_crash_after_dispatch_is_unknown_on_next_call(self):
        with patch('ipo_monitor.mention_delivery.request_api',side_effect=RuntimeError('simulated crash')):
            with self.assertRaises(RuntimeError):self.call()
        self.assertEqual(self.receipts()[0][0],'sending')
        with patch('ipo_monitor.mention_delivery.request_api',side_effect=AssertionError('retry')):
            self.assertEqual(self.call()['status'],'unknown')
        self.assertEqual(self.receipts()[0][:2],('unknown',1))

    def test_known_rejection_only_once_daily_max_three_attempts(self):
        self.catalog(end='2026-11-30')
        with patch('ipo_monitor.mention_delivery.request_api',side_effect=TelegramError('telegram_http_403')) as request:
            for day in (17,18,19,20):
                today=date(2026,11,day)
                with sqlite3.connect(self.path) as conn:
                    conn.execute("UPDATE mention_settings SET value=? WHERE key='catalog_date'",(today.isoformat(),))
                first=deliver_events(self.path,credential=CREDS,allow_send=True,today=today)
                self.assertEqual(first['status'],'failed')
                deliver_events(self.path,credential=CREDS,allow_send=True,today=today)
            self.assertEqual(request.call_count,3)
        self.assertEqual(self.receipts()[0][:2],('failed',3))

    def test_separate_events_paced_and_first_failure_stops_call(self):
        self.add_event(kind='youtube')
        events=[]
        def request(*args):events.append('send');return {'message_id':len(events)}
        with patch('ipo_monitor.mention_delivery.request_api',side_effect=request),patch('ipo_monitor.mention_delivery.time.sleep',side_effect=lambda seconds:events.append(seconds)):
            result=self.call()
        self.assertEqual(result['sent'],2)
        self.assertEqual(events,['send',1.05,'send'])

    def test_first_unknown_preserves_later_event_for_next_call(self):
        self.add_event(kind='youtube')
        with patch('ipo_monitor.mention_delivery.request_api',side_effect=TelegramError('telegram_network_error',uncertain=True)) as request:
            result=self.call();self.assertEqual(request.call_count,1)
            self.assertEqual(result['pending'],1)
        with patch('ipo_monitor.mention_delivery.request_api',return_value={'message_id':42}) as request:
            result=self.call();self.assertEqual(result['sent'],1)
            self.assertEqual(request.call_count,1)
        self.assertEqual([r[0] for r in self.receipts()],['unknown','sent'])

    def test_only_current_month_catalog_and_unexpired_events(self):
        self.catalog(start='2026-11-15',end='2026-11-16')
        self.catalog(stock='다음달기업',month='2026-12',start='2026-12-01',end='2026-12-02')
        self.add_event(stock='다음달기업')
        with patch('ipo_monitor.mention_delivery.request_api',side_effect=AssertionError('send')):
            result=self.call()
        self.assertEqual(result['status'],'empty');self.assertEqual(result['expired'],1)
        self.assertEqual(result['not_in_catalog'],1)
        self.assertFalse(self.path.with_suffix('.delivery.lock').is_symlink())
        with sqlite3.connect(self.path) as conn:
            self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE name='mention_delivery'").fetchone())

    def test_subscription_end_day_is_included(self):
        self.catalog(start='2026-11-16',end='2026-11-17')
        with patch('ipo_monitor.mention_delivery.request_api',return_value={'message_id':42}):
            self.assertEqual(self.call()['sent'],1)

    def test_stale_catalog_is_fail_closed(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE mention_settings SET value='2026-11-16' WHERE key='catalog_date'")
        with patch('ipo_monitor.mention_delivery.request_api',side_effect=AssertionError('send')):
            result=self.call()
        self.assertEqual(result['status'],'stale_catalog');self.assertEqual(result['sent'],0)

    def test_pending_fixture_and_site_events_are_not_sent(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE mention_events SET analysis_status='pending'")
        self.add_event(origin='fixture');self.add_event(kind='ipo_site')
        with patch('ipo_monitor.mention_delivery.request_api',side_effect=AssertionError('send')):
            result=self.call()
        self.assertEqual(result['pending'],1);self.assertEqual(result['skipped_fixture'],1)
        self.assertEqual(result['skipped_source'],1)

    def test_untrusted_url_or_invalid_evidence_never_sent(self):
        with sqlite3.connect(self.path) as conn:
            source=json.loads(conn.execute('SELECT input_json FROM mention_events').fetchone()[0])
            source['url']='https://evil.invalid/fake'
            conn.execute('UPDATE mention_events SET input_json=?',(json.dumps(source),))
        with patch('ipo_monitor.mention_delivery.request_api',side_effect=AssertionError('send')):
            self.assertEqual(self.call()['invalid'],1)

    def test_control_characters_are_collapsed_in_one_line_message(self):
        with sqlite3.connect(self.path) as conn:
            result=json.loads(conn.execute('SELECT analysis_json FROM mention_events').fetchone()[0])
            result['reason']='\x1b[31m청약에 참여한다고 밝혔습니다.'
            conn.execute('UPDATE mention_events SET analysis_json=?',(json.dumps(result),))
        preview=deliver_events(self.path,dry_run=True,today=TODAY)
        message=preview['messages'][0]
        self.assertEqual(message.count('\n'),1);self.assertNotIn('\x1b',message)
        self.assertIn('청약에 참여한다고 밝혔습니다.',message)

    def test_fabricated_quote_is_invalid_and_never_sent(self):
        with sqlite3.connect(self.path) as conn:
            result=json.loads(conn.execute('SELECT analysis_json FROM mention_events').fetchone()[0])
            result['evidence']='참여하겠습니다.'
            conn.execute('UPDATE mention_events SET analysis_json=?',(json.dumps(result),))
        with patch('ipo_monitor.mention_delivery.request_api',side_effect=AssertionError('send')):
            preview=deliver_events(self.path,dry_run=True,today=TODAY)
        self.assertEqual(preview['messages'],[]);self.assertEqual(preview['invalid'],1)

    def test_reason_policy_rejections_are_counted_invalid_and_never_sent(self):
        cases=(('가'*181,'too_long'),('첫째입니다. 둘째입니다.','multiple_sentences'),
               ('','empty'),('공모가 4만원에 참여합니다.','fabricated_amount'),
               ('11월 21일 마감입니다.','fabricated_date'))
        for reason,label in cases:
            with self.subTest(label):
                with sqlite3.connect(self.path) as conn:
                    result=json.loads(conn.execute('SELECT analysis_json FROM mention_events').fetchone()[0])
                    result['reason']=reason
                    conn.execute('UPDATE mention_events SET analysis_json=?',(json.dumps(result),))
                with patch('ipo_monitor.mention_delivery.request_api',side_effect=AssertionError('send')):
                    preview=deliver_events(self.path,dry_run=True,today=TODAY)
                self.assertEqual(preview['messages'],[],label)
                self.assertEqual(preview['invalid'],1,label)

    def test_closed_control_events_are_skipped_and_counted(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute('''CREATE TABLE mention_event_controls(event_id INTEGER PRIMARY KEY,
                closed INTEGER NOT NULL DEFAULT 0,analysis_retry_at TEXT)''')
            conn.execute('INSERT INTO mention_event_controls VALUES (1,1,NULL)')
        with patch('ipo_monitor.mention_delivery.request_api',side_effect=AssertionError('send')):
            result=self.call()
        self.assertEqual(result['closed'],1);self.assertEqual(result['sent'],0)
        self.assertEqual(result['status'],'empty')
        with sqlite3.connect(self.path) as conn:
            self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE name='mention_delivery'").fetchone())

    def test_send_requires_opt_in_and_missing_file_not_created(self):
        with self.assertRaises(CollectionError):deliver_events(self.path,credential=CREDS,today=TODAY)
        missing=self.path.parent/'missing.sqlite3'
        self.assertEqual(deliver_events(missing,credential=CREDS,allow_send=True,today=TODAY)['status'],'empty')
        self.assertFalse(missing.exists())

    def test_invalid_credentials_or_date_rejected_without_network(self):
        with patch('ipo_monitor.mention_delivery.request_api',side_effect=AssertionError('send')):
            for credential in ({'token':'bad','chat_id':'123'}, {'token':TOKEN,'chat_id':'-123'}):
                with self.assertRaises(CollectionError):
                    deliver_events(self.path,credential=credential,allow_send=True,today=TODAY)
            with self.assertRaises(CollectionError):self.call(today='2026-11-17')
