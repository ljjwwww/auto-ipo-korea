from contextlib import redirect_stdout,redirect_stderr
from datetime import datetime
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
from ipo_monitor.mention_workflow import EventAnalyzer,event_cache_key

TODAY=datetime(2026,11,17,19,tzinfo=ZoneInfo('Asia/Seoul'))


class MentionWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        shutil.copytree('samples/assessment',self.root/'sample')
        shutil.copy('config/decision.toml',self.root/'policy.toml')
        (self.root/'telegram.toml').write_text('[telegram]\nproject_root="."\nenabled=false\nstate_db="unused.sqlite3"\n')
        self.config=self.root/'daily.toml'
        self.config.write_text('''[daily]
project_root="."
workflow="mention_events"
event_db="events.sqlite3"
enabled=true
backend="fixture"
model="fixture-model"
offline=true
sources="sample/sources.toml"
db="sources.sqlite3"
ledger="ledger.sqlite3"
aliases="sample/stocks.toml"
policy="policy.toml"
fixture="sample/responses.json"
log="daily.jsonl"
max_calls_per_day=600
max_attempts=3
retry_hours=20
telegram_config="telegram.toml"
''')

    def call(self,*extra):
        out=io.StringIO()
        with redirect_stdout(out),redirect_stderr(out),patch('ipo_monitor.daily.now',return_value=TODAY),patch('ipo_monitor.daily_state._now',return_value=TODAY.isoformat()),patch('ipo_monitor.mention_delivery._today',return_value=TODAY.date()):
            result=main(['daily','--config',str(self.config),*extra])
        return result,out.getvalue()

    def summary(self,out):return json.loads(out.splitlines()[-1])

    def test_baseline_no_archive_blast_then_single_new_event_then_silence(self):
        def response(h,source):
            return {'opinion':'참여','assessment':'positive','stage':'planned',
                    'evidence':source['content']['text'][:60],'reason':'새 글에서 청약 참여 의견을 밝혔습니다.'}
        with patch('httpx.post',side_effect=AssertionError('HTTP')),patch('httpx.stream',side_effect=AssertionError('HTTP')),patch('ipo_monitor.credentials.read_key',side_effect=AssertionError('secret')):
            code,out=self.call();self.assertEqual(code,0,out)
            first=self.summary(out);self.assertTrue(first['discovery']['baseline'])
            self.assertEqual(first['requests'],0)
            self.assertEqual(first['discovery']['queued'],0)
            self.assertEqual(first['discovery']['catalog'],3)
            post=self.root/'sample/blog.xml'
            post.write_text(post.read_text().replace('가상기업은 청약에 참여하겠습니다.','가상기업은 청약에 참여하겠습니다. 새 의견입니다.'))
            with patch('ipo_monitor.mention_workflow.FixtureAnalyzer.analyze',side_effect=response):
                code,out=self.call();self.assertEqual(code,0,out)
                second=self.summary(out);self.assertEqual(second['discovery']['queued'],1)
                self.assertEqual(second['requests'],1)
                self.assertEqual(second['telegram']['status'],'disabled')
                code,out=self.call();self.assertEqual(code,0,out)
                self.assertEqual(self.summary(out)['requests'],0)
                self.assertEqual(self.summary(out)['discovery']['queued'],0)
        with sqlite3.connect(self.root/'events.sqlite3') as conn:
            self.assertEqual(conn.execute('select count(*) from mention_events').fetchone()[0],1)
            self.assertEqual(conn.execute('select analysis_status,analysis_origin from mention_events').fetchone(),('success','fixture'))
        with sqlite3.connect(self.root/'sources.sqlite3') as conn:
            self.assertFalse(conn.execute("select name from sqlite_master where name='decision_versions'").fetchone())

    def test_dryrun_does_not_collect_read_keys_or_create_databases(self):
        with patch('ipo_monitor.mention_workflow.collect_sources',side_effect=AssertionError('collect')),patch('ipo_monitor.credentials.read_key',side_effect=AssertionError('secret')),patch('ipo_monitor.telegram_credentials.read_credentials',side_effect=AssertionError('secret')):
            code,out=self.call('--dry-run');self.assertEqual(code,0,out)
        self.assertFalse((self.root/'sources.sqlite3').exists())
        self.assertFalse((self.root/'events.sqlite3').exists())
        self.assertFalse((self.root/'ledger.sqlite3').exists())

    def test_event_model_cache_identity_matches_actual_prompt_not_old_prompt(self):
        from ipo_monitor.ollama import cache_key
        analyzer=EventAnalyzer('test-model',True,600,'test-secret')
        self.assertEqual(analyzer.key('x'),event_cache_key('test-model'))
        self.assertNotEqual(analyzer.key('x'),cache_key('test-model'))

    def test_mock_live_backend_end_to_end_only_new_mention_sends_once(self):
        import httpx
        from ipo_monitor import CollectionError
        self.config.write_text(self.config.read_text().replace('backend="fixture"','backend="ollama"').replace('fixture="sample/responses.json"\n',''))
        telegram=self.root/'telegram.toml';telegram.write_text(telegram.read_text().replace('enabled=false','enabled=true'))
        blog=self.root/'sample/blog.xml'
        blog.write_text(blog.read_text().replace('https://example.invalid/blog/','https://blog.naver.com/hycj1025/22400000000'))
        credential={'token':'123456789:'+'x'*35,'chat_id':'1234567'}
        calls=[]
        def http(url,**kwargs):
            if url.startswith('https://ollama.com/'):
                source=json.loads(kwargs['json']['messages'][1]['content'])
                result={'opinion':'참여','assessment':'positive','stage':'planned',
                        'evidence':source['content']['text'][:60],
                        'reason':'블로거가 이 종목 청약에 참여하겠다는 새 의견을 밝혔습니다.'}
                payload={'done':True,'done_reason':'stop','message':{'role':'assistant','content':json.dumps(result)},'prompt_eval_count':100,'eval_count':50}
                calls.append('AI')
            elif url.startswith('https://api.telegram.org/'):
                calls.append('Telegram')
                self.assertIn('[가상기업]',kwargs['json']['text'])
                self.assertNotIn('가상대기기업',kwargs['json']['text'])
                payload={'ok':True,'result':{'message_id':42}}
            else:raise AssertionError('unexpected host')
            return httpx.Response(200,json=payload,request=httpx.Request('POST',url))
        with patch('ipo_monitor.credentials.read_key',return_value='fake-test-key'),patch('ipo_monitor.telegram_credentials.read_credentials',return_value=credential),patch('httpx.post',side_effect=http):
            code,out=self.call('--allow-api');self.assertEqual(code,0,out)
            self.assertEqual(calls,[])
            blog.write_text(blog.read_text().replace('가상기업은 청약에 참여하겠습니다.','가상기업은 청약에 참여하겠습니다. 추가 의견입니다.'))
            code,out=self.call('--allow-api');self.assertEqual(code,0,out)
            self.assertEqual(calls,['AI','Telegram'])
            self.assertEqual(self.summary(out)['telegram']['sent'],1)
            code,out=self.call('--allow-api');self.assertEqual(code,0,out)
            self.assertEqual(calls,['AI','Telegram'])
            self.assertEqual(self.summary(out)['requests'],0)
            self.assertEqual(self.summary(out)['telegram']['sent'],0)
