"""Decision reason protocol and durable rejected-response diagnostics."""
from contextlib import closing
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from ipo_monitor import CollectionError
from ipo_monitor.event_reasons import Attempt, ReasonCode, save_attempt
from ipo_monitor.event_validation import validate_event_response
from ipo_monitor.assessment import FixtureAnalyzer
from ipo_monitor.mention_workflow import EventAnalyzer, EventFixtureAnalyzer
from ipo_monitor.mentions import ensure_schema
from tests.test_event_validation import response, source


class EventReasonTests(unittest.TestCase):
    def test_coded_fixture_works_only_through_the_event_protocol(self):
        text = '가상기업은 경쟁률이 낮으면 참여하겠습니다.'
        coded = {**response(text, '경쟁률을 확인한 뒤 참여할 예정입니다.', opinion='조건부'),
                 'reason_code': 2}
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'response.json'
            path.write_text(json.dumps({'input': coded}, ensure_ascii=False))
            self.assertEqual(EventFixtureAnalyzer(path).analyze('input', source(text)), coded)
            with self.assertRaises(CollectionError):
                FixtureAnalyzer(path).analyze('input', source(text))

    def test_reason_codes_reject_unknown_boolean_and_inconsistent_opinions(self):
        text = '가상기업은 경쟁률이 낮으면 참여하겠습니다.'
        valid = {**response(text, '경쟁률을 확인한 뒤 참여할 예정입니다.', opinion='조건부'),
                 'reason_code': 2}
        self.assertIs(validate_event_response(valid, source(text)), valid)
        for code in (True, 99, '2', 0):
            with self.subTest(code=code), self.assertRaises(CollectionError) as caught:
                validate_event_response({**valid, 'reason_code': code}, source(text))
            self.assertEqual(str(caught.exception), 'event_reason_code_invalid')

    def test_live_event_requires_reason_code_and_keeps_reply_on_rejection(self):
        text = '가상기업은 청약에 참여하겠습니다.'
        answer = response(text)
        raw = json.dumps(answer, ensure_ascii=False)
        result = httpx.Response(200, json={'done': True, 'message': {
            'content': raw}}, request=httpx.Request('POST', 'https://ollama.com/api/chat'))
        analyzer = EventAnalyzer('test-model', True, 1, 'fake-key')
        with patch('httpx.post', return_value=result), self.assertRaises(CollectionError) as caught:
            analyzer.analyze('input', source(text))
        self.assertEqual(str(caught.exception), 'event_reason_code_invalid')
        self.assertEqual(analyzer.last_response_text, raw)

    def test_transport_error_never_reuses_previous_response_reason(self):
        text = '가상기업은 청약에 참여하겠습니다.'
        raw = json.dumps({**response(text), 'reason_code': 0}, ensure_ascii=False)
        good = httpx.Response(200, json={'done': True, 'message': {'content': raw}},
                              request=httpx.Request('POST', 'https://ollama.com/api/chat'))
        analyzer = EventAnalyzer('test-model', True, 2, 'fake-key')
        with patch('httpx.post', side_effect=[good, httpx.ConnectError('offline')]):
            analyzer.analyze('first', source(text))
            with self.assertRaises(CollectionError):
                analyzer.analyze('second', source(text))
        self.assertIsNone(analyzer.last_response_text)

    def test_legacy_response_remains_readable_without_inventing_a_code(self):
        text = '가상기업은 청약에 참여하겠습니다.'
        legacy = response(text)
        self.assertIs(validate_event_response(legacy, source(text)), legacy)
        self.assertNotIn('reason_code', legacy)

    def test_rejected_reason_is_saved_with_error_without_promoting_event(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            ensure_schema(conn)
            conn.execute("""INSERT INTO mention_events
                (id,event_hash,stock,source_id,source_type,item_id,snapshot_id,
                 input_hash,input_json,created_at,analysis_status)
                VALUES (1,'event','가상기업','blog','rss','item',1,'input','{}','now','error')""")
            raw = json.dumps({'opinion': '조건부', 'reason_code': 2,
                              'reason': '경쟁률 확인 뒤 검토할 예정입니다.'}, ensure_ascii=False)
            save_attempt(conn, Attempt(1, 'input', 'backend', 'test-model',
                                      raw, 'event_reason_number_ungrounded'))
            row = conn.execute("""SELECT reason_code,reason_text,model,error_code,response_text
                FROM mention_analysis_attempts""").fetchone()
            self.assertEqual(row, (ReasonCode.CONDITION_UNCONFIRMED,
                                   '경쟁률 확인 뒤 검토할 예정입니다.', 'test-model',
                                   'event_reason_number_ungrounded', raw))
            self.assertEqual(conn.execute('SELECT analysis_status,analysis_json FROM mention_events').fetchone(),
                             ('error', None))

    def test_transport_error_has_no_fabricated_ai_reason(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            ensure_schema(conn)
            save_attempt(conn, Attempt(1, 'input', 'backend', 'test-model', None,
                                      'ollama_network_error'))
            self.assertEqual(conn.execute("""SELECT reason_code,reason_text,response_text,error_code
                FROM mention_analysis_attempts""").fetchone(),
                (None, None, None, 'ollama_network_error'))
