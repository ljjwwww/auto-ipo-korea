"""Offline extraction acceptance tests: fixed responses are not real model calls."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape

import ipo_monitor


def candidate(name, opinion, evidence, basis="author_opinion"):
    return dict(name=name, opinion=opinion, evidence=evidence, basis=basis)


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.feed = self.root / "feed.xml"
        self.db = self.root / "posts.sqlite3"
        self.fixture = self.root / "responses.json"

    def call(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = ipo_monitor.main(list(args))
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue()

    def collect(self, *posts):
        entries = []
        for number, title, summary in posts:
            entries.append(
                f'<item><guid isPermaLink="false">p{number}</guid>'
                f'<title>{escape(title)}</title>'
                f'<link>https://example.invalid/{number}</link>'
                '<pubDate>Sat, 14 Nov 2026 10:00:00 +0900</pubDate>'
                f'<description>{escape(summary)}</description></item>'
            )
        self.feed.write_text(
            '<rss version="2.0"><channel><title>샘플</title>'
            '<link>https://example.invalid/</link><description>검증</description>'
            + ''.join(entries) + '</channel></rss>', encoding='utf-8')
        return self.call('--feed', str(self.feed), '--db', str(self.db))

    def query(self, sql):
        with sqlite3.connect(self.db) as connection:
            return connection.execute(sql).fetchall()

    def fixture_for(self, responses):
        from ipo_monitor.extraction import input_hash

        with sqlite3.connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            posts = connection.execute('SELECT * FROM posts').fetchall()
        values = {
            post['link']: {'input_hash': input_hash(dict(post)), 'response': responses[post['entry_id']]}
            for post in posts
        }
        self.fixture.write_text(json.dumps(values, ensure_ascii=False), encoding='utf-8')

    def extract(self):
        return self.call('extract', '--db', str(self.db), '--backend', 'fixture',
                         '--fixture', str(self.fixture))

    def listing(self):
        return self.call('candidates', '--db', str(self.db))

    def assert_success(self, result, text=None):
        code, stdout, stderr = result
        self.assertEqual(code, 0, stderr)
        if text:
            self.assertIn(text, stdout)
        return stdout

    def prepare_one(self):
        self.assert_success(self.collect((1, '가상기업 공모주', '가상기업 청약에 참여합니다.')))
        self.fixture_for({'p1': {'status': 'relevant', 'candidates': [
            candidate('가상기업', '참여', '가상기업 청약에 참여합니다.')]}})

    def test_multiple_stocks_keep_conditional_and_pass_opinions_separate(self):
        self.assert_success(self.collect((1, '공모주 검토',
            '가상알파 가격이 낮으면 참여합니다. 가상베타 청약은 패스합니다.')))
        self.fixture_for({'p1': {'status': 'relevant', 'candidates': [
            candidate('가상알파', '조건부', '가상알파 가격이 낮으면 참여합니다.'),
            candidate('가상베타', '패스', '가상베타 청약은 패스합니다.')]}})
        self.assert_success(self.extract(), '추출 호출: 1건, 재사용: 0건, 실패: 0건')
        output = self.assert_success(self.listing())
        for text in ('가상알파', '조건부', '가상베타', '패스', 'https://example.invalid/1', '2026-11-14'):
            self.assertIn(text, output)
        self.assertEqual(self.query('SELECT name, opinion FROM candidate_results ORDER BY name'),
                         [('가상베타', '패스'), ('가상알파', '조건부')])

    def test_simple_comparison_mention_is_unknown_not_participation(self):
        self.assert_success(self.collect((1, '공모주 비교', '가상감마는 비교 대상으로만 언급합니다.')))
        self.fixture_for({'p1': {'status': 'relevant', 'candidates': [
            candidate('가상감마', '판단 불가', '가상감마는 비교 대상으로만 언급합니다.', 'mention')]}})
        self.assert_success(self.extract())
        output = self.assert_success(self.listing())
        self.assertIn('판단 불가', output)
        self.assertEqual(self.query('SELECT opinion FROM candidate_results'), [('판단 불가',)])

    def test_mention_cannot_claim_participation(self):
        self.prepare_one()
        self.fixture_for({'p1': {'status': 'relevant', 'candidates': [
            candidate('가상기업', '참여', '가상기업 청약에 참여합니다.', 'mention')]}})
        self.assertNotEqual(self.extract()[0], 0)
        self.assertEqual(self.query('SELECT COUNT(*) FROM candidate_results'), [(0,)])

    def test_insufficient_content_and_irrelevant_post_are_distinct(self):
        self.assert_success(self.collect(
            (1, '가상델타 공모주', '가상델타 자세한 의견은 본문에서 확인하세요.'),
            (2, '일상 산책', '오늘은 공원에서 산책했습니다.')))
        self.fixture_for({
            'p1': {'status': 'insufficient', 'candidates': [candidate(
                '가상델타', '판단 불가', '가상델타 자세한 의견은 본문에서 확인하세요.', 'insufficient')]},
            'p2': {'status': 'irrelevant', 'candidates': []},
        })
        self.assert_success(self.extract())
        self.assertEqual(self.query('SELECT result_status FROM extraction_runs ORDER BY entry_id'),
                         [('insufficient',), ('irrelevant',)])
        output = self.assert_success(self.listing())
        self.assertIn('가상델타', output)
        self.assertIn('판단 불가', output)

    def test_unchanged_input_reuses_success_without_duplicate_candidates(self):
        self.prepare_one()
        self.assert_success(self.extract(), '추출 호출: 1건, 재사용: 0건, 실패: 0건')
        self.assert_success(self.extract(), '추출 호출: 0건, 재사용: 1건, 실패: 0건')
        self.assertEqual(self.query('SELECT COUNT(*) FROM extraction_runs'), [(1,)])
        self.assertEqual(self.query('SELECT COUNT(*) FROM candidate_results'), [(1,)])
        self.assertEqual(self.query('SELECT attempts FROM extraction_runs'), [(1,)])

    def test_changed_summary_updates_existing_post_and_reextracts_current_opinion(self):
        self.prepare_one()
        self.assert_success(self.extract())
        self.assert_success(self.collect((1, '가상기업 공모주', '가상기업 청약은 패스합니다.')), '새 글: 0건')
        self.assertEqual(self.query('SELECT COUNT(*) FROM posts'), [(1,)])
        self.assertEqual(self.query('SELECT summary FROM posts'), [('가상기업 청약은 패스합니다.',)])
        self.fixture_for({'p1': {'status': 'relevant', 'candidates': [
            candidate('가상기업', '패스', '가상기업 청약은 패스합니다.')]}})
        self.assert_success(self.extract(), '추출 호출: 1건, 재사용: 0건, 실패: 0건')
        output = self.assert_success(self.listing())
        self.assertIn('패스', output)
        self.assertNotIn('가상기업 청약에 참여합니다.', output)

    def test_different_posts_for_same_company_preserve_both_sources(self):
        self.assert_success(self.collect(
            (1, '가상기업 1차', '가상기업 참여합니다.'),
            (2, '가상기업 후속', '가상기업 패스합니다.')))
        self.fixture_for({
            'p1': {'status': 'relevant', 'candidates': [candidate('가상기업', '참여', '가상기업 참여합니다.')]},
            'p2': {'status': 'relevant', 'candidates': [candidate('가상기업', '패스', '가상기업 패스합니다.')]},
        })
        self.assert_success(self.extract())
        output = self.assert_success(self.listing())
        self.assertIn('https://example.invalid/1', output)
        self.assertIn('https://example.invalid/2', output)
        self.assertIn('참여', output)
        self.assertIn('패스', output)
        self.assertEqual(self.query('SELECT COUNT(*) FROM candidate_results'), [(2,)])

    def test_schema_error_retries_without_marking_success(self):
        self.prepare_one()
        self.fixture_for({'p1': {'status': 'relevant', 'candidates': 'not-an-array'}})
        for attempt in (1, 2):
            code, stdout, _ = self.extract()
            self.assertNotEqual(code, 0)
            self.assertIn('추출 호출: 1건, 재사용: 0건, 실패: 1건', stdout)
            self.assertEqual(self.query('SELECT status, attempts FROM extraction_runs'), [('error', attempt)])
            self.assertEqual(self.query('SELECT COUNT(*) FROM candidate_results'), [(0,)])

    def test_corrected_response_can_succeed_after_schema_failure(self):
        self.prepare_one()
        self.fixture_for({'p1': {'status': 'invalid', 'candidates': []}})
        self.assertNotEqual(self.extract()[0], 0)
        self.fixture_for({'p1': {'status': 'relevant', 'candidates': [
            candidate('가상기업', '참여', '가상기업 청약에 참여합니다.')]}})
        self.assert_success(self.extract())
        self.assertIn('참여', self.assert_success(self.listing()))
        self.assertEqual(self.query("SELECT COUNT(*) FROM extraction_runs WHERE status='success'"), [(1,)])

    def test_unquoted_evidence_is_rejected(self):
        self.prepare_one()
        self.fixture_for({'p1': {'status': 'relevant', 'candidates': [
            candidate('가상기업', '참여', '원문에 없는 가짜 근거')]}})
        self.assertNotEqual(self.extract()[0], 0)
        self.assertEqual(self.query('SELECT COUNT(*) FROM candidate_results'), [(0,)])

    def test_fixture_cannot_be_applied_to_changed_input(self):
        self.prepare_one()
        self.assert_success(self.collect((1, '가상기업 공모주', '가상기업 청약 의견은 아직 없습니다.')))
        self.assertNotEqual(self.extract()[0], 0)
        self.assertEqual(self.query('SELECT COUNT(*) FROM candidate_results'), [(0,)])

    def test_original_schema_data_is_preserved_when_extraction_tables_are_added(self):
        # Collection uses the original feeds/posts schema, without extraction tables.
        self.prepare_one()
        before = self.query('SELECT * FROM posts')
        self.assert_success(self.extract())
        self.assertEqual(self.query('SELECT * FROM posts'), before)
        self.assert_success(self.call('--feed', str(self.feed), '--db', str(self.db)), '새 글: 0건')

    def test_openai_cannot_call_without_explicit_allow_flag_even_with_key(self):
        self.prepare_one()
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'secret-test-key'}), patch('httpx.post') as post:
            result = self.call('extract', '--db', str(self.db), '--backend', 'openai')
        self.assertNotEqual(result[0], 0)
        post.assert_not_called()
        self.assertNotIn('secret-test-key', result[1] + result[2])

    def test_fixture_extraction_does_not_need_key_or_network(self):
        self.prepare_one()
        with patch.dict(os.environ, {}, clear=True), patch('httpx.post') as post, patch('httpx.get') as get:
            self.assert_success(self.extract())
            self.assert_success(self.listing())
        post.assert_not_called()
        get.assert_not_called()

    def test_html_summary_quotes_are_validated_against_readable_text(self):
        self.assert_success(self.collect((1, '가상기업 공모주', '<p>가상기업 <b>청약에 참여합니다.</b></p>')))
        self.fixture_for({'p1': {'status': 'relevant', 'candidates': [
            candidate('가상기업', '참여', '가상기업 청약에 참여합니다.')]}})
        self.assert_success(self.extract())
        self.assertIn('가상기업 청약에 참여합니다.', self.assert_success(self.listing()))

    def test_name_missing_from_source_is_rejected(self):
        self.prepare_one()
        self.fixture_for({'p1': {'status': 'relevant', 'candidates': [
            candidate('존재하지않는기업', '참여', '가상기업 청약에 참여합니다.')]}})
        self.assertNotEqual(self.extract()[0], 0)
        self.assertEqual(self.query('SELECT COUNT(*) FROM candidate_results'), [(0,)])

    def test_failed_changed_input_does_not_show_old_opinion_as_current(self):
        self.prepare_one()
        self.assert_success(self.extract())
        self.assert_success(self.collect((1, '가상기업 정정', '가상기업 의견은 다시 검토합니다.')))
        self.fixture_for({'p1': {'status': 'relevant', 'candidates': 'broken'}})
        self.assertNotEqual(self.extract()[0], 0)
        output = self.assert_success(self.listing())
        self.assertNotIn('가상기업 청약에 참여합니다.', output)

    def test_extending_fixture_for_new_post_reuses_unchanged_existing_post(self):
        self.prepare_one()
        self.assert_success(self.extract(), '추출 호출: 1건, 재사용: 0건, 실패: 0건')
        self.assert_success(self.collect(
            (1, '가상기업 공모주', '가상기업 청약에 참여합니다.'),
            (2, '신규기업 공모주', '신규기업 청약은 패스합니다.')), '새 글: 1건')
        self.fixture_for({
            'p1': {'status': 'relevant', 'candidates': [
                candidate('가상기업', '참여', '가상기업 청약에 참여합니다.')]},
            'p2': {'status': 'relevant', 'candidates': [
                candidate('신규기업', '패스', '신규기업 청약은 패스합니다.')]},
        })
        self.assert_success(self.extract(), '추출 호출: 1건, 재사용: 1건, 실패: 0건')
        self.assertEqual(self.query('SELECT COUNT(*) FROM extraction_runs'), [(2,)])
        self.assertEqual(self.query('SELECT name, COUNT(*) FROM candidate_results GROUP BY name ORDER BY name'),
                         [('가상기업', 1), ('신규기업', 1)])
        self.assertEqual(self.query("SELECT attempts FROM extraction_runs WHERE entry_id='p1'"), [(1,)])

    def test_restored_successful_fixture_is_selected_after_other_fixture_error(self):
        self.prepare_one()
        original_fixture = self.fixture.read_text(encoding='utf-8')
        self.assert_success(self.extract())
        self.fixture_for({'p1': {'status': 'relevant', 'candidates': 'broken'}})
        self.assertNotEqual(self.extract()[0], 0)
        self.assertIn('추출 오류', self.assert_success(self.listing()))
        self.fixture.write_text(original_fixture, encoding='utf-8')
        self.assert_success(self.extract(), '추출 호출: 0건, 재사용: 1건, 실패: 0건')
        output = self.assert_success(self.listing())
        self.assertIn('가상기업 청약에 참여합니다.', output)
        self.assertNotIn('추출 오류', output)
        self.assertEqual(self.query('SELECT COUNT(*) FROM candidate_results'), [(1,)])
        self.assertEqual(self.query("SELECT attempts FROM extraction_runs WHERE status='success'"), [(1,)])

    def test_previous_extraction_schema_migrates_selection_timestamp_without_data_loss(self):
        from ipo_monitor.extraction import input_hash

        self.prepare_one()
        timestamp = '2026-11-14T10:00:00+09:00'
        with sqlite3.connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            post = dict(connection.execute('SELECT * FROM posts').fetchone())
            connection.execute('''CREATE TABLE extraction_runs (
                id INTEGER PRIMARY KEY, feed_id TEXT NOT NULL, entry_id TEXT NOT NULL,
                input_hash TEXT NOT NULL, extractor_key TEXT NOT NULL,
                status TEXT NOT NULL, result_status TEXT, error_code TEXT,
                attempts INTEGER NOT NULL DEFAULT 1, extracted_at TEXT NOT NULL,
                UNIQUE(feed_id,entry_id,input_hash,extractor_key))''')
            connection.execute('''INSERT INTO extraction_runs
                (feed_id,entry_id,input_hash,extractor_key,status,error_code,extracted_at)
                VALUES (?,?,?,?,?,?,?)''',
                (post['feed_id'], post['entry_id'], input_hash(post), 'fixture:legacy',
                 'error', 'schema_invalid', timestamp))
        original_posts = self.query('SELECT * FROM posts')
        self.assertIn('추출 오류', self.assert_success(self.listing()))
        self.assertEqual(self.query('SELECT * FROM posts'), original_posts)
        self.assertEqual(self.query('SELECT status,error_code,extracted_at,selected_at FROM extraction_runs'),
                         [('error', 'schema_invalid', timestamp, timestamp)])
        self.assert_success(self.extract())
        self.assertIn('가상기업 청약에 참여합니다.', self.assert_success(self.listing()))


class OpenAITransportTests(unittest.TestCase):
    """Mock the wire boundary; never send a paid request."""

    def setUp(self):
        self.source = {
            'title': '가상기업 공모주', 'summary': '가상기업 청약에 참여합니다.',
            'link': 'https://example.invalid/1', 'published_raw': '', 'published_at': None,
        }
        self.result = {'status': 'relevant', 'candidates': [
            candidate('가상기업', '참여', '가상기업 청약에 참여합니다.')]}
        self.key = 'secret-key-never-log'

    def extractor(self):
        from ipo_monitor.extraction import OpenAIExtractor

        with patch.dict(os.environ, {'OPENAI_API_KEY': self.key}):
            return OpenAIExtractor('configurable-test-model', allow_api=True)

    def response(self, payload=None, *, status=200, content=None):
        import httpx

        request = httpx.Request('POST', 'https://api.openai.com/v1/responses')
        if content is not None:
            return httpx.Response(status, content=content, request=request)
        return httpx.Response(status, json=payload, request=request)

    def completed(self, result=None):
        return {'status': 'completed', 'output': [
            {'type': 'reasoning', 'summary': []},
            {'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(
                self.result if result is None else result, ensure_ascii=False)}]},
        ]}

    def test_structured_request_contract_and_completed_result_validation(self):
        from ipo_monitor.extraction import SCHEMA, validate_result

        with patch('httpx.post', return_value=self.response(self.completed())) as post:
            result = self.extractor().extract(self.source)
        self.assertEqual(validate_result(result, self.source), self.result)
        args, kwargs = post.call_args
        self.assertEqual(args, ('https://api.openai.com/v1/responses',))
        body = kwargs['json']
        self.assertEqual(body['model'], 'configurable-test-model')
        self.assertIs(body['store'], False)
        self.assertEqual(body['text']['format'], {
            'type': 'json_schema', 'name': 'ipo_opinions', 'strict': True, 'schema': SCHEMA})
        self.assertEqual(json.loads(body['input']), self.source)
        self.assertNotIn(self.key, json.dumps(body))
        self.assertEqual(kwargs['headers']['Authorization'], f'Bearer {self.key}')
        self.assertGreater(kwargs['timeout'], 0)
        self.assertIn('신뢰할 수 없는 원문 데이터', body['instructions'])

    def test_refusal_is_not_treated_as_a_success(self):
        from ipo_monitor.extraction import ExtractionError

        payload = {'status': 'completed', 'output': [{'type': 'message', 'content': [
            {'type': 'refusal', 'refusal': f'private refusal {self.key}'}]}]}
        with patch('httpx.post', return_value=self.response(payload)):
            with self.assertRaisesRegex(ExtractionError, '^api_refusal$'):
                self.extractor().extract(self.source)

    def test_incomplete_response_is_not_parsed_as_partial_success(self):
        from ipo_monitor.extraction import ExtractionError

        payload = self.completed()
        payload['status'] = 'incomplete'
        with patch('httpx.post', return_value=self.response(payload)):
            with self.assertRaisesRegex(ExtractionError, '^api_incomplete$'):
                self.extractor().extract(self.source)

    def test_invalid_json_response_is_safe_error(self):
        from ipo_monitor.extraction import ExtractionError

        with patch('httpx.post', return_value=self.response(content=self.key.encode())):
            with self.assertRaisesRegex(ExtractionError, '^api_response_invalid$'):
                self.extractor().extract(self.source)

    def test_malformed_outer_response_shapes_are_safe_errors(self):
        from ipo_monitor.extraction import ExtractionError

        payloads = [[], None, 'private text', {'status': 'completed', 'output': None},
                    {'status': 'completed', 'output': ['not-a-message']},
                    {'status': 'completed', 'output': [{'type': 'message', 'content': None}]}]
        for payload in payloads:
            with self.subTest(payload=payload), patch('httpx.post', return_value=self.response(payload)):
                with self.assertRaisesRegex(ExtractionError, '^api_response_invalid$'):
                    self.extractor().extract(self.source)

    def test_non_json_model_text_is_safe_error(self):
        from ipo_monitor.extraction import ExtractionError

        payload = self.completed()
        payload['output'][1]['content'][0]['text'] = f'not json {self.key}'
        with patch('httpx.post', return_value=self.response(payload)):
            with self.assertRaisesRegex(ExtractionError, '^api_response_invalid$'):
                self.extractor().extract(self.source)

    def test_http_error_never_exposes_response_body_or_key(self):
        from ipo_monitor.extraction import ExtractionError

        with patch('httpx.post', return_value=self.response(status=429, content=self.key.encode())):
            with self.assertRaisesRegex(ExtractionError, '^api_http_429$'):
                self.extractor().extract(self.source)

    def test_network_timeout_never_exposes_exception_text(self):
        import httpx
        from ipo_monitor.extraction import ExtractionError

        with patch('httpx.post', side_effect=httpx.ReadTimeout(self.key)):
            with self.assertRaisesRegex(ExtractionError, '^api_network_error$'):
                self.extractor().extract(self.source)

    def test_missing_key_blocks_authorized_mode_before_any_request(self):
        from ipo_monitor.extraction import ExtractionError, OpenAIExtractor

        with patch.dict(os.environ, {}, clear=True), patch('httpx.post') as post:
            with self.assertRaisesRegex(ExtractionError, '^api_key_missing:'):
                OpenAIExtractor('test-model', allow_api=True)
        post.assert_not_called()

    def test_completed_schema_invalid_model_response_is_rejected(self):
        from ipo_monitor.extraction import ExtractionError, validate_result

        with patch('httpx.post', return_value=self.response(self.completed({'wrong': 'shape'}))):
            result = self.extractor().extract(self.source)
        with self.assertRaisesRegex(ExtractionError, '^schema_invalid$'):
            validate_result(result, self.source)


if __name__ == '__main__':
    unittest.main()
