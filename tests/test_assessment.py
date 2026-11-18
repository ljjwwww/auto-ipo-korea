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
from ipo_monitor.multisource import connect
from ipo_monitor.evidence import build_inputs


class AssessmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);shutil.copytree('samples/assessment',self.root/'sample')
        self.sample=self.root/'sample';self.db=self.root/'db.sqlite3'
        self.policy=self.root/'policy.toml';shutil.copy('config/decision.toml',self.policy)
        self.call('collect','--offline','--config',str(self.sample/'sources.toml'),'--db',str(self.db))
    def call(self,*args):
        out=io.StringIO()
        with redirect_stdout(out),redirect_stderr(out):code=main(list(args))
        return code,out.getvalue()
    def assess(self,*extra):
        return self.call('assess','--db',str(self.db),'--fixture',str(self.sample/'responses.json'),
          '--aliases',str(self.sample/'stocks.toml'),'--policy',str(self.policy),*extra)
    def query(self,command,*extra):
        return self.call(command,'--db',str(self.db),'--aliases',str(self.sample/'stocks.toml'),
                         '--policy',str(self.policy),*extra)
    def rows(self,sql):
        with sqlite3.connect(self.db) as c:return c.execute(sql).fetchall()
    def test_whole_flow_no_network_and_repeat_no_duplicate(self):
        with patch('httpx.post',side_effect=AssertionError('AI')),patch('httpx.get',side_effect=AssertionError('network')),patch('httpx.stream',side_effect=AssertionError('network')):
            code,out=self.assess();self.assertEqual(code,0,out);self.assertIn('고정 응답 호출 9건',out)
            self.assertIn('가상기업: 검토 후보',out);self.assertIn('가상대기기업: 판단 대기',out);self.assertIn('가상상충기업: 추가 확인',out)
            code,out=self.assess();self.assertEqual(code,0,out);self.assertIn('호출 0건 · 재사용 9건',out)
            self.assertIn('판단 버전 신규 0건',out);self.assertIn('미리보기 신규 0건',out)
        self.assertEqual(self.rows('select count(*) from observation_analyses'),[(9,)])
        self.assertEqual(self.rows('select count(*) from decision_versions'),[(3,)])
        self.assertEqual(self.rows('select count(*) from notification_outbox'),[(3,)])
        code,out=self.query('notification-preview');self.assertEqual(code,0,out)
        self.assertIn('[테스트/미발송]',out);self.assertIn('실제 AI 분석',out)
    def test_change_policy_keeps_analysis_cache_and_versions_policy_snapshot(self):
        self.assess();self.policy.write_text(self.policy.read_text().replace('candidate = 0.4','candidate = 0.8'))
        code,out=self.query('notification-preview');self.assertEqual(code,0,out);self.assertIn('미리보기 0건',out)
        code,out=self.assess();self.assertEqual(code,0,out);self.assertIn('호출 0건 · 재사용 9건',out)
        self.assertEqual(self.rows('select count(*) from decision_versions'),[(6,)])
        r=json.loads(self.rows('select result_json from decision_versions order by id desc limit 1')[0][0])
        self.assertEqual(r['policy_snapshot']['thresholds']['candidate'],0.8)
    def test_source_change_makes_old_preview_stale_until_reanalysis(self):
        self.assess();p=self.sample/'youtube.json';items=json.loads(p.read_text())
        items[0]['title']+=' 수정';p.write_text(json.dumps(items,ensure_ascii=False))
        self.call('collect','--offline','--config',str(self.sample/'sources.toml'),'--db',str(self.db))
        code,out=self.query('decisions','--stock','가상기업');self.assertIn('재분석 필요',out)
        code,out=self.query('notification-preview','--stock','가상기업');self.assertIn('미리보기 0건',out)
        code,out=self.assess();self.assertEqual(code,1,out);self.assertIn('추가 확인',out)
        self.assertEqual(self.rows('select count(*) from decision_versions'),[(4,)])
    def test_invalid_schema_and_ungrounded_quote_retry(self):
        p=self.sample/'responses.json';r=json.loads(p.read_text());key=next(iter(r));r[key]['evidence']='원문에 없는 허위 문구';p.write_text(json.dumps(r))
        for _ in range(2):
            code,out=self.assess();self.assertEqual(code,1,out)
        self.assertEqual(self.rows("select attempts from observation_analyses where status='error'"),[(2,)])
    def test_common_hash_independent_of_local_snapshot_id_and_collection_time(self):
        with connect(self.db) as c:
            before=build_inputs(c,self.sample/'stocks.toml')
            c.execute("update source_snapshots set observed_at='2026-11-16T00:00:00+09:00'")
            after=build_inputs(c,self.sample/'stocks.toml')
        self.assertEqual([e['input_hash'] for e in before],[e['input_hash'] for e in after])
        self.assertEqual(len(before),9)
    def test_export_path_limited_to_data(self):
        code,out=self.query('evidence-inputs','--output',str(self.root/'export.json'))
        self.assertEqual(code,1,out);self.assertFalse((self.root/'export.json').exists())
    def test_alias_collision_rejected(self):
        p=self.sample/'stocks.toml';p.write_text(p.read_text()+'\n[[stocks]]\nname="別企業"\naliases=["DEMO"]\n')
        code,out=self.assess();self.assertEqual(code,1,out);self.assertIn('겹치는',out)
    def test_messenger_channels_separate_without_actual_send(self):
        self.assess();code,out=self.assess('--channel','slack');self.assertEqual(code,0,out)
        self.assertIn('미리보기 신규 3건',out)
        self.assertEqual(self.rows('select distinct status from notification_outbox'),[('preview',)])

    def test_source_error_reuses_analysis_but_holds_result_and_versions(self):
        self.assess()
        with sqlite3.connect(self.db) as c:
            c.execute("insert into source_attempts (source_id,started_at,finished_at,status,error,item_count,new_count,changed_count,unchanged_count) values ('sample-girit-site','x','y','error','test failure',0,0,0,0)")
        code,out=self.assess();self.assertEqual(code,0,out)
        self.assertIn('호출 0건 · 재사용 9건',out)
        code,out=self.query('decisions','--stock','가상기업')
        self.assertIn('추가 확인',out);self.assertIn('test failure',out)

    def test_fixed_response_change_invalidates_only_target_analysis(self):
        self.assess();p=self.sample/'responses.json';values=json.loads(p.read_text());key=next(iter(values))
        values[key]['assessment']='negative';p.write_text(json.dumps(values,ensure_ascii=False))
        code,out=self.assess();self.assertEqual(code,0,out)
        self.assertIn('호출 1건 · 재사용 8건',out)
        self.assertEqual(self.rows('select count(*) from decision_versions'),[(4,)])

    def test_changed_input_does_not_erase_previous_decision_provenance(self):
        self.assess()
        with sqlite3.connect(self.db) as c:
            result=json.loads(c.execute('select result_json from decision_versions limit 1').fetchone()[0])
        self.assertIn('policy_snapshot',result)
        for contribution in result['contributions']:
            self.assertIn('snapshot_id',contribution);self.assertIn('input_hash',contribution)
            self.assertIn('analysis_id',contribution);self.assertIn('url',contribution)

    def test_repeated_collection_same_values_does_not_reanalyze(self):
        self.assess()
        self.call('collect','--offline','--config',str(self.sample/'sources.toml'),'--db',str(self.db))
        code,out=self.assess();self.assertEqual(code,0,out)
        self.assertIn('호출 0건 · 재사용 9건',out)
        self.assertIn('판단 버전 신규 0건',out)

    def test_newest_published_blog_is_not_shadowed_by_old_post_inserted_later(self):
        p=self.sample/'blog.xml';text=p.read_text()
        old='<item><guid>older</guid><title>가상기업 과거 의견</title><link>https://example.invalid/blog/older</link><pubDate>2026-10-01T10:00:00+09:00</pubDate><description>가상기업은 청약하지 않겠습니다.</description></item>'
        p.write_text(text.replace('</channel>',old+'</channel>'))
        self.call('collect','--offline','--config',str(self.sample/'sources.toml'),'--db',str(self.db))
        with connect(self.db) as c:entries=build_inputs(c,self.sample/'stocks.toml')
        responses=json.loads((self.sample/'responses.json').read_text())
        for e in entries:
            if e['input']['url'].endswith('/older'):
                responses[e['input_hash']]={'opinion':'미참여','assessment':'unknown','evidence':'가상기업은 청약하지 않겠습니다.','stage':'planned','reason':'가상 과거 의견'}
        (self.sample/'responses.json').write_text(json.dumps(responses))
        code,out=self.assess();self.assertEqual(code,0,out)
        results=[json.loads(r[0]) for r in self.rows("select result_json from decision_versions where stock='가상기업'")]
        selected=[c for c in results[-1]['contributions'] if c['source_type']=='rss' and c['selected']]
        self.assertEqual(selected[0]['opinion'],'참여')
        self.assertFalse(next(c for c in results[-1]['contributions'] if c['url'].endswith('/older'))['selected'])

    def test_policy_and_source_reversion_append_transitions_not_duplicate_analysis(self):
        self.assess();original=self.policy.read_text()
        self.policy.write_text(original.replace('candidate = 0.4','candidate = 0.8'));self.assess()
        self.policy.write_text(original);code,out=self.assess();self.assertEqual(code,0,out)
        self.assertIn('상태 전이 신규 3건',out);self.assertIn('미리보기 신규 3건',out)
        self.assertEqual(self.rows('select count(*) from decision_versions'),[(6,)])
        self.assertEqual(self.rows('select count(*) from decision_transitions'),[(9,)])
        self.assertEqual(self.rows('select count(*) from notification_outbox'),[(9,)])
        code,out=self.assess();self.assertIn('상태 전이 신규 0건',out)
        code,out=self.query('notification-preview');self.assertIn('미발송 미리보기 3건',out)

    def test_filtered_history_survives_removed_manual_stock_mention(self):
        p=self.sample/'blog.xml';original=p.read_text();p.write_text(original.replace('가상기업','별도기업'))
        self.call('collect','--offline','--config',str(self.sample/'sources.toml'),'--db',str(self.db))
        self.assess('--stock','별도기업')  # Missing fixture records a retryable analysis and hold decision.
        p.write_text(original)
        self.call('collect','--offline','--config',str(self.sample/'sources.toml'),'--db',str(self.db))
        code,out=self.query('decisions','--stock','별도기업','--history')
        self.assertEqual(code,0,out);self.assertIn('별도기업',out);self.assertIn('재분석 필요',out)
        code,out=self.query('notification-preview','--stock','별도기업','--history')
        self.assertIn('과거/입력 변경',out)

    def test_whitespace_is_not_valid_grounded_evidence(self):
        p=self.sample/'responses.json';original=json.loads(p.read_text())
        for blank in (' ','\n','\t '):
            values=json.loads(json.dumps(original));values[next(iter(values))]['evidence']=blank
            p.write_text(json.dumps(values))
            code,out=self.assess();self.assertEqual(code,1,out)
            self.assertIn('분석 실패 1건',out)

    def test_embedded_name_is_preserved_as_unconfirmed_not_scored(self):
        p=self.sample/'youtube.json';items=json.loads(p.read_text())
        items[0]['title']='다른 회사 이야기'
        items[0]['payload']['captions']['text']='가상기업아의 소식입니다.'
        items[0]['payload']['captions']['segments']=[{'start':'00:00:00.000','end':'00:00:01.000','text':'가상기업아의 소식입니다.'}]
        p.write_text(json.dumps(items))
        self.call('collect','--offline','--config',str(self.sample/'sources.toml'),'--db',str(self.db))
        code,out=self.assess();self.assertEqual(code,1,out)
        with connect(self.db) as c:
            entries=build_inputs(c,self.sample/'stocks.toml',['가상기업'])
        ambiguous=[e for e in entries if e['input']['source_type']=='youtube']
        self.assertEqual(ambiguous[0]['match_quality'],'substring_candidate')
        result=json.loads(self.rows("select result_json from decision_versions where stock='가상기업' order by id desc limit 1")[0][0])
        self.assertEqual(result['status'],'추가 확인')
        self.assertIn('analysis_target_ambiguous',result['analysis_issues'][0]['error'])

    def test_literal_entity_with_particles_and_nonmatching_prefix(self):
        from ipo_monitor.evidence import entity_match
        self.assertTrue(entity_match('엠비디는 청약을 검토한다.',['엠비디']))
        self.assertTrue(entity_match('엠비디 청약',['엠비디']))
        self.assertFalse(entity_match('엠비디아발 걱정',['엠비디']))
        self.assertFalse(entity_match('영광와이케이엠씨',['영광']))
