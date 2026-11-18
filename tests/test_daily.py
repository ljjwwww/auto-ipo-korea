from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from ipo_monitor import main
from ipo_monitor.daily_state import file_lock


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree('samples/assessment', self.root/'sample')
        shutil.copy('config/decision.toml', self.root/'policy.toml')
        self.config = self.root/'daily.toml'
        self.config.write_text('''[daily]
project_root = "."
enabled = true
backend = "fixture"
model = "fixture-model"
offline = true
sources = "sample/sources.toml"
db = "data.sqlite3"
ledger = "ledger.sqlite3"
aliases = "sample/stocks.toml"
policy = "policy.toml"
fixture = "sample/responses.json"
log = "logs/daily.jsonl"
max_calls_per_day = 6
max_attempts = 3
retry_hours = 20
''')
        self.current = datetime(2026,11,16,9,tzinfo=ZoneInfo('Asia/Seoul'))

    def call(self, *extra):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out), patch('ipo_monitor.daily.now', return_value=self.current), patch('ipo_monitor.daily_state._now', return_value=self.current.isoformat()):
            code = main(['daily', '--config', str(self.config), *extra])
        return code, out.getvalue()

    def summary(self, output):
        return json.loads(output.splitlines()[-1])

    def rows(self, name, sql):
        with sqlite3.connect(self.root/name) as conn:
            return conn.execute(sql).fetchall()

    def test_offline_full_flow_cap_repeat_next_day_no_send(self):
        with patch('httpx.post', side_effect=AssertionError('AI')), patch('httpx.stream', side_effect=AssertionError('network')):
            code, out = self.call(); self.assertEqual(code,0,out)
            self.assertEqual(self.summary(out)['requests'],6)
            self.assertEqual(self.summary(out)['pending'],3)
            self.assertEqual(len(self.rows('data.sqlite3','select * from observation_analyses')),6)
            code, out = self.call(); self.assertEqual(code,0,out)
            self.assertEqual(self.summary(out)['requests'],0)
            self.assertEqual(self.summary(out)['remaining'],0)
            self.current += timedelta(days=1)
            code, out = self.call(); self.assertEqual(code,0,out)
            self.assertEqual(self.summary(out)['requests'],3)
            self.assertEqual(self.summary(out)['pending'],0)
            self.assertEqual(self.summary(out)['status'],'success')
            count=len(self.rows('data.sqlite3','select * from notification_outbox'))
            code,out=self.call();self.assertEqual(code,0,out)
            self.assertEqual(self.summary(out)['requests'],0)
            self.assertEqual(len(self.rows('data.sqlite3','select * from notification_outbox')),count)
        self.assertEqual(self.rows('data.sqlite3','select distinct status from notification_outbox'),[('preview',)])

    def test_dryrun_no_key_collect_network_or_creating_db(self):
        with patch('ipo_monitor.daily.collect_sources',side_effect=AssertionError('collect')),patch('ipo_monitor.credentials.read_key',side_effect=AssertionError('key')):
            code,out=self.call('--dry-run');self.assertEqual(code,0,out)
        self.assertFalse((self.root/'data.sqlite3').exists())
        self.assertFalse((self.root/'ledger.sqlite3').exists())
        self.call()
        with patch('ipo_monitor.daily.collect_sources',side_effect=AssertionError('collect')),patch('ipo_monitor.credentials.read_key',side_effect=AssertionError('key')):
            code,out=self.call('--dry-run');self.assertEqual(code,0,out)
            self.assertEqual(self.summary(out)['pending'],3)
        self.assertEqual(len(self.rows('ledger.sqlite3','select * from daily_runs')),1)

    def test_disabled_and_missing_allow_api_have_no_collection(self):
        self.config.write_text(self.config.read_text().replace('backend = "fixture"','backend = "ollama"'))
        with patch('ipo_monitor.daily.collect_sources',side_effect=AssertionError('collect')):
            code,out=self.call();self.assertEqual(code,1,out)
            self.assertIn('--allow-api',out)
            self.config.write_text(self.config.read_text().replace('enabled = true','enabled = false'))
            code,out=self.call('--allow-api');self.assertEqual(code,0,out)
            self.assertEqual(self.summary(out)['status'],'disabled')
        self.assertFalse((self.root/'ledger.sqlite3').exists())

    def test_lock_contention_no_collection(self):
        with file_lock(self.root/'ledger.lock'),patch('ipo_monitor.daily.collect_sources',side_effect=AssertionError('collect')):
            code,out=self.call();self.assertEqual(code,1,out)
            self.assertIn('잠금',out)

    def test_failures_wait_cooldown_then_exhaust_and_new_inputs_first(self):
        self.config.write_text(self.config.read_text().replace('max_calls_per_day = 6','max_calls_per_day = 20'))
        (self.root/'sample/responses.json').write_text('{}')
        for index in range(3):
            code,out=self.call();self.assertEqual(code,1,out)
            self.assertEqual(self.summary(out)['requests'],9)
            self.assertEqual(self.summary(out)['exhausted'],9 if index==2 else 0)
            code,out=self.call();self.assertEqual(code,0,out)
            self.assertEqual(self.summary(out)['requests'],0)
            self.current += timedelta(days=1)
        code,out=self.call();self.assertEqual(code,0,out)
        self.assertEqual(self.summary(out)['requests'],0)
        self.assertEqual(self.summary(out)['exhausted'],9)
        self.assertEqual(self.rows('ledger.sqlite3','select count(*) from daily_reservations'),[(27,)])
        self.assertNotIn('analysis_fixture_missing',(self.root/'logs/daily.jsonl').read_text())

    def test_bad_config_rejected_before_side_effects(self):
        for old,new in [('max_calls_per_day = 6','max_calls_per_day = true'),('retry_hours = 20','retry_hours = 0'),('ledger = "ledger.sqlite3"','ledger = "data.sqlite3"')]:
            original=self.config.read_text();self.config.write_text(original.replace(old,new))
            code,out=self.call();self.assertEqual(code,1,out)
            self.config.write_text(original)
        self.assertFalse((self.root/'data.sqlite3').exists())

    def test_new_inputs_before_retry_and_cap_across_model_change(self):
        (self.root/'sample/responses.json').write_text('{}')
        code,out=self.call();self.assertEqual(code,1,out)
        self.assertEqual(self.summary(out)['requests'],6)
        self.config.write_text(self.config.read_text().replace('fixture-model','other-model'))
        code,out=self.call();self.assertEqual(code,0,out)
        self.assertEqual(self.summary(out)['requests'],0)
        self.config.write_text(self.config.read_text().replace('other-model','fixture-model'))
        self.current += timedelta(days=1)
        code,out=self.call();self.assertEqual(code,1,out)
        self.assertEqual(self.summary(out)['requests'],6)
        with sqlite3.connect(self.root/'ledger.sqlite3') as conn:
            first = [r[0] for r in conn.execute('select input_hash from daily_reservations where day="2026-11-16"')]
            second = [r[0] for r in conn.execute('select input_hash from daily_reservations where day="2026-11-17"')]
        self.assertEqual(len(set(second)-set(first)),3)

    def test_lazy_ollama_requests_failures_still_consume_daily_budget(self):
        from ipo_monitor import CollectionError
        class FailedOllama:
            def __init__(self,*args): self.usage=[]
            def analyze(self,h,source):
                self.usage.append(dict(input_hash=h, model='fixture-model', input_tokens=None,
                                       output_tokens=None, http_status=None, thinking_used=None))
                raise CollectionError('ollama_request_failed')
        self.config.write_text(self.config.read_text().replace('backend = "fixture"','backend = "ollama"'))
        with patch('ipo_monitor.credentials.read_key',return_value='mock-not-a-key') as key,patch('ipo_monitor.ollama.OllamaAnalyzer',FailedOllama):
            code,out=self.call('--allow-api');self.assertEqual(code,1,out)
            self.assertEqual(self.summary(out)['requests'],6)
            self.assertEqual(key.call_count,1)
            code,out=self.call('--allow-api');self.assertEqual(code,0,out)
            self.assertEqual(self.summary(out)['requests'],0)
            self.assertEqual(key.call_count,1)
        self.assertEqual(self.rows('ledger.sqlite3',"select count(*) from daily_reservations where state='failed'"),[(6,)])

    def test_collection_failure_sets_operational_exit_but_keeps_work(self):
        from ipo_monitor.daily import collect_sources
        def partial(args):
            collect_sources(args)
            return 1
        with patch('ipo_monitor.daily.collect_sources',side_effect=partial):
            code,out=self.call()
        self.assertEqual(code,1,out)
        result=self.summary(out)
        self.assertEqual(result['status'],'error')
        self.assertEqual(result['error_code'],'collection_failed')
        self.assertEqual(result['phase'],'collection')
        self.assertEqual(result['requests'],6)

    def test_exception_after_dispatch_reports_reservations_without_raw_error(self):
        from ipo_monitor.daily import plan_entries
        with patch('ipo_monitor.daily.plan_entries',side_effect=[None, ValueError('secret-source-body')]) as plan:
            # First planner must return real queue; second raises after requests are durable.
            def dispatch(*args):
                if plan.call_count == 1:
                    return plan_entries(*args)
                raise ValueError('secret-source-body')
            plan.side_effect=dispatch
            code,out=self.call()
        self.assertEqual(code,1,out)
        result=self.summary(out)
        self.assertEqual(result['requests'],6)
        self.assertEqual(result['error_code'],'validation_error')
        self.assertEqual(result['phase'],'queue_refresh')
        self.assertNotIn('secret-source-body',out)
        self.assertNotIn('secret-source-body',(self.root/'logs/daily.jsonl').read_text())
        stored=json.loads(self.rows('ledger.sqlite3','select summary_json from daily_runs')[0][0])
        self.assertEqual(stored['requests'],6)

    def test_usage_from_previous_request_not_copied_to_local_failure(self):
        from ipo_monitor import CollectionError
        from ipo_monitor.daily import DailyAnalyzer,load_config
        from ipo_monitor.daily_state import DailyLedger
        class LocalFailure:
            def __init__(self): self.usage=[]
            def analyze(self,h,source):
                if h == 'second':
                    raise CollectionError('local_input_invalid')
                self.usage.append(dict(input_hash=h,input_tokens=111,output_tokens=22,http_status=200))
                return {}
        with DailyLedger(self.root/'ledger.sqlite3') as ledger:
            run_id=ledger.start_run(self.current.date().isoformat())
            analyzer=DailyAnalyzer(load_config(self.config),['first','second'],ledger,run_id,self.current.date().isoformat())
            analyzer.delegate=LocalFailure()
            with patch('ipo_monitor.daily.now',return_value=self.current):
                analyzer.analyze('first',{})
                with self.assertRaises(CollectionError): analyzer.analyze('second',{})
            rows=ledger.conn.execute('select input_tokens,output_tokens,http_status,state from daily_reservations order by id').fetchall()
            self.assertEqual(tuple(rows[0]),(111,22,200,'completed'))
            self.assertEqual(tuple(rows[1]),(None,None,None,'failed'))

    def test_600_limit_supported_and_601_rejected(self):
        from ipo_monitor.daily import load_config
        from ipo_monitor import CollectionError
        self.config.write_text(self.config.read_text().replace('max_calls_per_day = 6','max_calls_per_day = 600'))
        self.assertEqual(load_config(self.config)['max_calls_per_day'],600)
        code,out=self.call()
        self.assertEqual(code,0,out)
        self.assertEqual(self.summary(out)['requests'],9)
        self.assertEqual(self.summary(out)['remaining'],591)
        self.config.write_text(self.config.read_text().replace('max_calls_per_day = 600','max_calls_per_day = 601'))
        with self.assertRaises(CollectionError):load_config(self.config)

    def test_runtime_deadline_defers_without_request_or_key(self):
        from ipo_monitor.daily import DailyAnalyzer,load_config
        from ipo_monitor.daily_state import DailyLedger
        from ipo_monitor.assessment import AnalysisDeferred
        config=load_config(self.config);config['_deadline']=5
        with DailyLedger(self.root/'ledger.sqlite3') as ledger:
            run=ledger.start_run('2026-11-16')
            analyzer=DailyAnalyzer(config,['h'],ledger,run,'2026-11-16')
            with patch('ipo_monitor.daily.time.monotonic',return_value=5),patch('ipo_monitor.credentials.read_key',side_effect=AssertionError('key')):
                with self.assertRaises(AnalysisDeferred):analyzer.analyze('h',{})
            self.assertEqual(analyzer.calls,0)
            self.assertEqual(ledger.remaining('2026-11-16',600),600)
