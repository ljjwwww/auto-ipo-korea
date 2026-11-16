from contextlib import redirect_stdout,redirect_stderr
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ipo_monitor import main


class MultiSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        shutil.copytree('samples/multisource',self.root/'fixtures')
        self.config=self.root/'fixtures/sources.toml';self.db=self.root/'db.sqlite3'
        self.args=['collect','--offline','--config',str(self.config),'--db',str(self.db)]
    def call(self,*args):
        out=io.StringIO()
        with redirect_stdout(out),redirect_stderr(out):
            code=main(list(args))
        return code,out.getvalue()
    def collect(self):return self.call(*self.args)
    def rows(self,sql):
        with sqlite3.connect(self.db) as conn:return conn.execute(sql).fetchall()
    def test_offline_three_sources_repeat_without_network_or_api(self):
        with patch('httpx.get',side_effect=AssertionError('network')),patch('httpx.stream',side_effect=AssertionError('network')),patch('httpx.post',side_effect=AssertionError('AI')):
            code,out=self.collect();self.assertEqual(code,0,out)
            self.assertEqual(out.count('신규 1건'),3)
            code,out=self.collect();self.assertEqual(code,0,out)
            self.assertEqual(out.count('동일 1건'),3)
        self.assertEqual(self.rows('select count(*) from source_snapshots'),[(3,)])
        self.assertEqual(self.rows('select count(*) from posts'),[(1,)])
        self.assertEqual(self.rows('select count(*) from source_attempts'),[(6,)])
    def test_changes_append_including_return_to_old_content(self):
        self.collect()
        path=self.config.parent/'site.json';original=path.read_text()
        items=json.loads(original);items[0]['payload']['events'][0]['stars']=3
        path.write_text(json.dumps(items))
        code,out=self.collect();self.assertEqual(code,0,out);self.assertIn('변경 1건',out)
        path.write_text(original)
        code,out=self.collect();self.assertEqual(code,0,out);self.assertIn('변경 1건',out)
        self.assertEqual(self.rows("select count(*) from source_snapshots where source_id='demo-site'"),[(3,)])
    def test_source_failure_continues_and_recovery(self):
        p=self.config.parent/'youtube.json';original=p.read_text();p.write_text('not JSON')
        code,out=self.collect();self.assertEqual(code,1,out)
        self.assertEqual(self.rows('select count(*) from source_snapshots'),[(2,)])
        p.write_text(original);code,out=self.collect();self.assertEqual(code,0,out)
        self.assertEqual(self.rows('select count(*) from source_snapshots'),[(3,)])
    def test_partial_caption_failure_keeps_metadata(self):
        p=self.config.parent/'youtube.json';items=json.loads(p.read_text())
        items[0]['payload']['captions']={'status':'error','error':'test'}
        p.write_text(json.dumps(items));code,out=self.collect()
        self.assertEqual(code,1,out);self.assertIn('partial',out)
        self.assertEqual(self.rows('select count(*) from source_snapshots'),[(3,)])
    def test_alias_query_candidates_and_history(self):
        self.collect()
        aliases=self.root/'aliases.toml';aliases.write_text('[[stocks]]\nname="가상기업"\naliases=["DEMO"]\n')
        code,out=self.call('observations','--db',str(self.db),'--stock','DEMO','--aliases',str(aliases))
        self.assertEqual(code,0,out);self.assertIn('표시 3건',out);self.assertIn('연결 후보',out)
        code,out=self.call('source-status','--db',str(self.db));self.assertEqual(code,0,out);self.assertIn('demo-site',out)
    def test_latest_query_does_not_match_removed_old_text(self):
        self.collect();p=self.config.parent/'youtube.json';items=json.loads(p.read_text())
        items[0]['title']='다른 글';items[0]['payload']={'summary':'다른 내용'};p.write_text(json.dumps(items));self.collect()
        code,out=self.call('observations','--db',str(self.db),'--stock','가상기업')
        self.assertIn('표시 2건',out)
        code,out=self.call('observations','--db',str(self.db),'--stock','가상기업','--history')
        self.assertIn('표시 3건',out)
    def test_offline_missing_fixture_fails_before_fetch(self):
        self.config.write_text(self.config.read_text().replace('fixture = "blog.xml"',''))
        with patch('httpx.get') as fetch:
            code,out=self.collect();self.assertEqual(code,1);fetch.assert_not_called()
        self.assertFalse(self.db.exists())
    def test_duplicate_ids_rejected_before_fetch(self):
        self.config.write_text(self.config.read_text().replace('demo-site','demo-blog'))
        code,out=self.collect();self.assertEqual(code,1);self.assertFalse(self.db.exists())
    def test_reassigned_source_identity_rejected(self):
        self.collect();self.config.write_text(self.config.read_text().replace('https://example.invalid/rss','https://example.invalid/other'))
        code,out=self.collect();self.assertEqual(code,1);self.assertIn('새 ID',out)
        self.assertEqual(self.rows('select count(*) from source_snapshots'),[(3,)])
    def test_snapshot_omission_does_not_delete_history(self):
        self.collect();(self.config.parent/'site.json').write_text('[]');self.collect()
        self.assertEqual(self.rows("select count(*) from source_snapshots where source_id='demo-site'"),[(1,)])
    def test_legacy_rss_command_and_results_still_work(self):
        self.collect()
        self.assertEqual(self.rows('select title from posts'),[('가상기업 공모주 검토',)])
        code,out=self.call('pipeline-results','--db',str(self.db));self.assertEqual(code,0,out)
        self.assertIn('미분석',out)

    def test_malformed_caption_status_is_source_error_not_crash(self):
        p=self.config.parent/'youtube.json';items=json.loads(p.read_text())
        items[0]['payload']['captions']=[];p.write_text(json.dumps(items))
        code,out=self.collect();self.assertEqual(code,1,out)
        self.assertEqual(self.rows('select count(*) from source_snapshots'),[(2,)])

    def test_query_warns_if_latest_source_fetch_failed(self):
        self.collect();(self.config.parent/'site.json').write_text('broken');self.collect()
        code,out=self.call('observations','--db',str(self.db),'--source','demo-site')
        self.assertEqual(code,0,out);self.assertIn('마지막 출처 수집 error',out)

    def test_duplicate_rss_guids_preserve_legacy_compatibility(self):
        p=self.config.parent/'blog.xml';text=p.read_text()
        item=text.split('<item>',1)[1].split('</item>',1)[0]
        p.write_text(text.replace('</channel>','<item>'+item+'</item></channel>'))
        code,out=self.collect();self.assertEqual(code,0,out)
        self.assertEqual(self.rows('select count(*) from posts'),[(1,)])
        self.assertEqual(self.rows("select count(*) from source_snapshots where source_id='demo-blog'"),[(1,)])

    def test_config_type_must_be_string_and_null_captions_fail_safely(self):
        original=self.config.read_text()
        self.config.write_text(original.replace('type = "rss"','type = ["rss"]'))
        self.assertEqual(self.collect()[0],1)
        self.config.write_text(original)
        p=self.config.parent/'youtube.json';items=json.loads(p.read_text())
        items[0]['payload']['captions']=None;p.write_text(json.dumps(items))
        code,out=self.collect();self.assertEqual(code,1,out)
