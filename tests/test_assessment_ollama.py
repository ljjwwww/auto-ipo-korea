import io
import json
from contextlib import redirect_stdout,redirect_stderr
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import httpx
from ipo_monitor import main


class OllamaIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.db=self.root/'db.sqlite3'
        shutil.copytree('samples/assessment',self.root/'sample');self.sample=self.root/'sample'
        self.call('collect','--offline','--config',str(self.sample/'sources.toml'),'--db',str(self.db))
    def call(self,*args):
        out=io.StringIO()
        with redirect_stdout(out),redirect_stderr(out):code=main(list(args))
        return code,out.getvalue()
    def args(self):
        return ['assess','--db',str(self.db),'--backend','ollama','--allow-api','--stock','가상기업',
                '--aliases',str(self.sample/'stocks.toml'),'--model','test-model','--max-calls','3']
    def response(self,*args,**kwargs):
        source=json.loads(kwargs['json']['messages'][1]['content'])
        result={'opinion':'판단 불가','assessment':'unknown','evidence':source['content']['text'].splitlines()[0],
                'stage':'unknown','reason':'가상 모의 응답'}
        return httpx.Response(200,json={'done':True,'done_reason':'stop','message':{'content':json.dumps(result)}},request=httpx.Request('POST','https://ollama.com/api/chat'))
    def test_no_authorization_or_no_stock_means_no_key_read_or_network(self):
        cases=[self.args()]
        cases[0].remove('--allow-api')
        no_stock=self.args();i=no_stock.index('--stock');del no_stock[i:i+2];cases.append(no_stock)
        for args in cases:
            with patch('ipo_monitor.credentials.read_key') as key,patch('httpx.post') as post:
                code,out=self.call(*args);self.assertEqual(code,1,out)
                key.assert_not_called();post.assert_not_called()
    def test_budget_preflight_blocks_all_calls_not_partial_silent_skips(self):
        args=self.args();args[-1]='2'
        with patch('ipo_monitor.credentials.read_key',return_value='mock-key'),patch('httpx.post') as post:
            code,out=self.call(*args)
        self.assertEqual(code,1,out);self.assertIn('필요한 요청 3건',out);post.assert_not_called()
        with sqlite3.connect(self.db) as c:self.assertEqual(c.execute('select count(*) from observation_analyses').fetchone()[0],0)
    def test_mock_ollama_call_cache_and_message_disclosure(self):
        with patch('ipo_monitor.credentials.read_key',return_value='mock-key'),patch('httpx.post',side_effect=self.response) as post:
            code,out=self.call(*self.args());self.assertEqual(code,0,out)
            self.assertEqual(post.call_count,3);self.assertIn('실제 Ollama 요청 3건',out)
            code,out=self.call(*self.args());self.assertEqual(code,0,out)
            self.assertEqual(post.call_count,3);self.assertIn('재사용 3건',out);self.assertIn('실제 Ollama 요청 0건',out)
        code,out=self.call('notification-preview','--db',str(self.db),'--aliases',str(self.sample/'stocks.toml'))
        self.assertEqual(code,0,out);self.assertIn('Ollama 실제 분석',out);self.assertNotIn('고정 응답 기반',out)
        self.assertNotIn('mock-key',out)
    def test_fixture_and_live_cache_are_separate(self):
        code,out=self.call('assess','--db',str(self.db),'--fixture',str(self.sample/'responses.json'),'--aliases',str(self.sample/'stocks.toml'))
        self.assertEqual(code,0,out)
        with patch('ipo_monitor.credentials.read_key',return_value='mock-key'),patch('httpx.post',side_effect=self.response) as post:
            code,out=self.call(*self.args());self.assertEqual(code,0,out);self.assertEqual(post.call_count,3)
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute("select count(*) from observation_analyses where backend_key like 'fixture:%'").fetchone()[0],9)
            self.assertEqual(c.execute("select count(*) from observation_analyses where backend_key like 'ollama:%'").fetchone()[0],3)
