import io
import json
from contextlib import redirect_stdout,redirect_stderr
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ipo_monitor import main,parse_feed
from ipo_monitor.extraction import source_input,digest


def result(category='ipo',opinion='판단 불가',stage='unknown'):
    return {'category':category,'candidates': [] if category!='ipo' else [
        {'name':'가상기업','opinion':opinion,'evidence':'가상기업 공모주',
         'basis':'insufficient' if opinion=='판단 불가' else 'author_opinion','stage':stage}]}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.db=self.root/'db.sqlite3'
        self.feed=self.root/'rss.xml'
        self.feed.write_text('<rss version="2.0"><channel><title>test</title><item><guid>1</guid><title>가상기업 공모주</title><link>https://example.invalid/1</link><description>정보 부족</description></item></channel></rss>')
        self.call('--feed',str(self.feed),'--db',str(self.db))
        self.source=source_input(parse_feed(self.feed.read_bytes())[0])
        self.fixture=self.root/'ai.json';self.bodies=self.root/'body.json'
        self.body='가상기업 공모주 청약에 참여했다.'
        self.bodies.write_text(json.dumps({self.source['link']:self.body}))
        self.responses={digest(self.source):result(),digest({**self.source,'body':self.body}):result(opinion='참여',stage='applied')}
        self.write()
    def write(self):
        self.fixture.write_text(json.dumps(self.responses,ensure_ascii=False))
    def call(self,*args):
        out=io.StringIO()
        with redirect_stdout(out),redirect_stderr(out):
            code=main(list(args))
        return code,out.getvalue()
    def run_pipeline(self,*extra):
        return self.call('pipeline','--skip-collect','--db',str(self.db),'--backend','fixture',
                         '--fixture',str(self.fixture),'--body-fixture',str(self.bodies),*extra)
    def test_fallback_then_cached_and_local_body(self):
        with patch('httpx.post',side_effect=AssertionError('no API')),patch('ipo_monitor.pipeline.fetch_body',side_effect=AssertionError('no fetch')):
            code,out=self.run_pipeline();self.assertEqual(code,0,out);self.assertIn('AI 추출 호출: 2건',out)
            self.assertIn('[본문]',out);self.assertIn('applied',out)
            code,out=self.run_pipeline();self.assertEqual(code,0,out);self.assertIn('AI 추출 호출: 0건, 재사용: 2건',out)
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute('select text from article_bodies').fetchone()[0],self.body)
            self.assertEqual(c.execute('select count(*) from semantic_runs').fetchone()[0],2)
    def test_property_and_conditional_do_not_fetch(self):
        for r in (result('property'),result(opinion='조건부',stage='planned')):
            self.responses[digest(self.source)]=r;self.write()
            with patch('ipo_monitor.pipeline.fetch_body',side_effect=AssertionError('not needed')):
                code,out=self.run_pipeline()
                self.assertEqual(code,0,out);self.assertNotIn('본문 추가 확인 필요',out)
    def test_body_missing_stays_unresolved_and_retryable(self):
        self.bodies.write_text('{}')
        code,out=self.run_pipeline();self.assertEqual(code,1);self.assertIn('판단 보류',out)
        self.bodies.write_text(json.dumps({self.source['link']:self.body}))
        code,out=self.run_pipeline();self.assertEqual(code,0,out);self.assertIn('재사용: 1건',out)
    def test_uncertain_without_candidates_fetches(self):
        self.responses[digest(self.source)]=result('uncertain');self.write()
        code,out=self.run_pipeline();self.assertEqual(code,0,out);self.assertIn('[본문]',out)
    def test_invalid_body_response_retries(self):
        self.responses[digest({**self.source,'body':self.body})]={'bad':'schema'};self.write()
        for _ in range(2):
            code,out=self.run_pipeline();self.assertEqual(code,1);self.assertIn('미참여로 간주하지',out)
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute("select attempts from semantic_runs where phase='본문'").fetchone()[0],2)
    def test_paid_backend_requires_opt_in(self):
        with patch('httpx.post') as post:
            code,out=self.call('pipeline','--skip-collect','--db',str(self.db))
            self.assertEqual(code,1);post.assert_not_called()

    def test_failed_refresh_is_retried_and_not_shown_as_current(self):
        self.assertEqual(self.run_pipeline()[0],0)
        self.bodies.write_text('{}')
        self.assertEqual(self.run_pipeline('--refresh-body')[0],1)
        code,out=self.run_pipeline()
        self.assertEqual(code,1)
        self.assertNotIn('[본문] 공모주',out)
        code,out=self.call('pipeline-results','--db',str(self.db))
        self.assertEqual(code,0)
        self.assertIn('수집 실패',out)
        self.assertNotIn('applied',out)
        self.bodies.write_text(json.dumps({self.source['link']:self.body}))
        self.assertEqual(self.run_pipeline()[0],0)

    def test_query_requires_no_backend_or_network(self):
        self.assertEqual(self.run_pipeline()[0],0)
        with patch('httpx.post',side_effect=AssertionError('no API')),patch('ipo_monitor.pipeline.fetch_body',side_effect=AssertionError('no fetch')):
            code,out=self.call('pipeline-results','--db',str(self.db))
        self.assertEqual(code,0)
        self.assertIn('[RSS]',out)
        self.assertIn('[본문]',out)
        self.assertIn('applied',out)
