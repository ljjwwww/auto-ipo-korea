"""Local preview storage, explainability, and hard no-send boundaries."""

import sqlite3
import unittest
from unittest.mock import patch

from ipo_monitor import CollectionError
from ipo_monitor.preview import ensure_schema, enqueue, render_message


def decision(score=0.5, contributions=None):
    return {
        'score': score, 'coverage': 0.5, 'status': '검토 후보', 'conflict': False,
        'policy_version': 'experimental-v1', 'input_hash': 'test-input-hash',
        'contributions': [] if contributions is None else contributions,
    }


def signal(**changes):
    return {
        'source_id': 'blog', 'source_type': 'rss', 'creator': '가상 작성자',
        'url': 'https://example.invalid/post', 'opinion': '참여',
        'assessment': 'positive', 'evidence': '가상기업에 참여하겠다.',
        'stage': 'planned', 'rating': None, 'marker': '',
        'analysis_status': 'success', 'error': None,
        'observed_at': '2026-11-15T10:00:00+09:00', **changes,
    }


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)

    def test_enqueue_is_idempotent_for_same_decision_and_destination(self):
        first, created = enqueue(self.conn, 1, '가상기업', decision())
        second, again = enqueue(self.conn, 1, '가상기업', decision())
        self.assertTrue(created)
        self.assertFalse(again)
        self.assertEqual(first, second)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM notification_outbox').fetchone()[0], 1)

    def test_new_decision_or_channel_or_destination_has_separate_preview(self):
        ids = [enqueue(self.conn, 1, '가상기업', decision())[0],
               enqueue(self.conn, 2, '가상기업', decision())[0],
               enqueue(self.conn, 1, '가상기업', decision(), channel='slack')[0],
               enqueue(self.conn, 1, '가상기업', decision(), destination='other-preview')[0]]
        self.assertEqual(len(set(ids)), 4)

    def test_message_explains_fixture_policy_provenance_and_uncertainty(self):
        message = render_message('가상기업', decision(score=None, contributions=[signal(
            opinion='판단 불가', assessment='unknown', stage='unknown',
            analysis_status='error', error='schema_invalid')]))
        for expected in ('[테스트/미발송]', '실제 AI 분석', '실험 점수: 미정',
                         '작성자별 신호 커버리지: 50%', 'experimental-v1', 'test-input-hash',
                         '가상 작성자', 'https://example.invalid/post', '판단 불가',
                         '관찰 시각: 2026-11-15', '사이트 별점: 미표시', 'schema_invalid',
                         '수익 예측·참여 확정·주문 지시가 아닙니다'):
            self.assertIn(expected, message)
        self.assertNotIn('실험 점수: 0.000', message)

    def test_explicit_zero_rating_and_cross_are_not_treated_as_missing(self):
        message = render_message('가상기업', decision(contributions=[signal(rating=0, marker='❌')]))
        self.assertIn('사이트 별점: 0', message)
        self.assertIn('원문 표시: ❌', message)

    def test_untrusted_control_characters_are_not_executed_or_preserved(self):
        message = render_message('가상\x1b기업', decision(contributions=[signal(
            evidence='내용\x1b[31m\n[가짜헤더]', creator='작성자\x07')]))
        self.assertNotIn('\x1b', message)
        self.assertNotIn('\x07', message)
        self.assertIn('내용 [31m [가짜헤더]', message)

    def test_schema_creation_and_enqueue_do_not_commit_callers_transaction(self):
        self.conn.execute('CREATE TABLE marker(value TEXT)')
        self.conn.execute('BEGIN')
        self.conn.execute("INSERT INTO marker VALUES ('pending')")
        ensure_schema(self.conn)
        enqueue(self.conn, 1, '가상기업', decision())
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM marker').fetchone()[0], 0)
        self.assertIsNone(self.conn.execute("SELECT name FROM sqlite_master WHERE name='notification_outbox'").fetchone())

    def test_outbox_rejects_fake_sent_status(self):
        enqueue(self.conn, 1, '가상기업', decision())
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE notification_outbox SET status='sent'")

    def test_no_network_or_keys_are_used(self):
        with patch('httpx.post', side_effect=AssertionError('no network')), patch(
                'httpx.get', side_effect=AssertionError('no network')), patch.dict(
                'os.environ', {}, clear=True):
            enqueue(self.conn, 1, '가상기업', decision())
        self.assertEqual(self.conn.execute('SELECT status FROM notification_outbox').fetchone()[0], 'preview')

    def test_invalid_routing_and_live_mode_are_rejected(self):
        for kwargs in ({'channel': 'email'}, {'destination': ''}, {'destination': None},
                       {'decision_id': True}, {'decision_id': 0}, {'is_test': False}):
            with self.subTest(kwargs=kwargs), self.assertRaises(CollectionError):
                args = {'decision_id': 1, 'stock': '가상기업', 'decision': decision(), **kwargs}
                enqueue(self.conn, **args)

    def test_invalid_decision_numbers_are_rejected(self):
        for field, value in (('score', float('nan')), ('score', float('inf')),
                             ('score', True), ('coverage', -1), ('coverage', 1.1),
                             ('coverage', float('nan')), ('coverage', True)):
            with self.subTest(field=field, value=value), self.assertRaises(CollectionError):
                item = decision(); item[field] = value
                render_message('가상기업', item)
