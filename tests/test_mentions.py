"""Offline-only catalogue and event detection, using synthetic source revisions."""
from datetime import date
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ipo_monitor.extraction import digest
from ipo_monitor.mentions import discover, ensure_schema, monthly_catalog
from ipo_monitor.multisource import connect


TODAY = date(2026, 11, 17)


class MentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.aliases = self.root/'stocks.toml'
        self.aliases.write_text('[[stocks]]\nname="가상기업"\naliases=["DEMO"]\n')
        self.source = connect(self.root/'sources.sqlite3'); self.addCleanup(self.source.close)
        self.state = sqlite3.connect(self.root/'events.sqlite3')
        self.state.row_factory = sqlite3.Row; self.addCleanup(self.state.close)
        for identifier, kind in [('site', 'ipo_site'), ('blog', 'rss'), ('video', 'youtube')]:
            self.source.execute('INSERT INTO tracked_sources VALUES (?,?,?,?,?)',
                (identifier, kind, identifier, '기릿' if kind != 'rss' else '팽', 'https://example.invalid/'+identifier))
        self.attempt('success')
        self.stock('가상기업', ['2026-11-18', '2026-11-19'])
        self.post('blog', 'old-blog', '가상기업 청약 정보')
        self.post('video', 'old-video', '가상기업 영상')
        self.source.commit()

    def attempt(self, status):
        self.source.execute('''INSERT INTO source_attempts
            (source_id,started_at,finished_at,status,error,item_count,new_count,changed_count,unchanged_count)
            VALUES ('site','start','finish',?,NULL,1,0,0,1)''', (status,))
        self.source.commit()

    def insert(self, source, item, title, payload):
        h = digest({'title': title, 'payload': payload})
        self.source.execute('''INSERT INTO source_snapshots
            (source_id,item_id,title,url,published_raw,published_at,payload_json,content_hash,observed_at)
            VALUES (?,?,?,?,?,?,?,?,?)''',
            (source, item, title, 'https://example.invalid/'+source+'/'+item,
             '2026-11-17', '2026-11-17T10:00:00+09:00' if source != 'site' else None,
             json.dumps(payload, ensure_ascii=False), h, '2026-11-17T19:00:00+09:00'))
        self.source.commit()

    def stock(self, name, days, *, fields=None, extra_events=None):
        events = [{'kind': 'subscription', 'date': day, 'raw_label': '청약) '+name,
                   'raw_marker': '', 'stars': None} for day in days]
        events.extend(extra_events or [])
        payload = {'stock_name': name, 'events': events,
                   'rating_observations': events,
                   'table_rows': [{'section': '청약예정', 'fields': fields}] if fields else []}
        self.insert('site', name, name, payload)

    def post(self, source, item, title, summary=''):
        payload = {'summary': summary}
        if source == 'video':
            payload['captions'] = {'status': 'available', 'language': 'ko',
                                   'text': summary, 'segments': []}
        self.insert(source, item, title, payload)

    def discover(self, today=TODAY):
        with patch('httpx.post', side_effect=AssertionError('AI/send')), patch('httpx.stream', side_effect=AssertionError('network')):
            return discover(self.source, self.aliases, self.state, today)

    def events(self):
        return self.state.execute('SELECT * FROM mention_events ORDER BY id').fetchall()

    def setting(self, name):
        row=self.state.execute('SELECT value FROM mention_settings WHERE key=?', (name,)).fetchone()
        return row[0] if row else None

    def test_first_discovery_baselines_all_current_sources_without_old_alerts(self):
        result = self.discover()
        self.assertTrue(result['baseline']); self.assertEqual(result['queued'], 0)
        self.assertEqual(result['catalog'], 1); self.assertEqual(result['seen'], 2)
        self.assertEqual(self.events(), [])
        self.assertEqual(self.setting('catalog_date'), '2026-11-17')
        again = self.discover()
        self.assertFalse(again['baseline']); self.assertEqual(again['changed'], 0)
        self.assertEqual(again['queued'], 0)

    def test_new_post_queues_literal_stock_and_keeps_input_provenance(self):
        self.discover()
        self.post('blog', 'new-blog', '가상기업은 청약 정보가 새로 나왔습니다.')
        result = self.discover()
        self.assertEqual(result['queued'], 1)
        event = self.events()[0]
        self.assertEqual((event['stock'], event['source_type'], event['item_id']), ('가상기업', 'rss', 'new-blog'))
        self.assertEqual(event['analysis_status'], 'pending')
        self.assertEqual(event['analysis_origin'], 'unknown')
        self.assertIsNone(event['analysis_json']); self.assertEqual(event['attempts'], 0)
        source = json.loads(event['input_json'])
        self.assertEqual(source['stock'], event['stock'])
        self.assertEqual(source['url'], 'https://example.invalid/blog/new-blog')
        self.assertEqual(digest(source), event['input_hash'])
        self.assertEqual(self.discover()['queued'], 0)

    def test_changed_post_and_video_caption_mention_are_events_not_unchanged_posts(self):
        self.discover()
        self.post('blog', 'old-blog', '가상기업 청약 정보', '가상기업에 참여하겠습니다.')
        self.post('video', 'other-video', '오늘 시장 정리', '가상기업은 조건을 확인한 뒤 참여합니다.')
        result = self.discover()
        self.assertEqual(result['queued'], 2)
        self.assertEqual({row['source_type'] for row in self.events()}, {'rss', 'youtube'})
        self.assertEqual(self.discover()['queued'], 0)
        self.post('blog', 'old-blog', '가상기업 청약 정보', '가상기업에 참여하겠습니다.')
        self.assertEqual(self.discover()['queued'], 0)

    def test_substring_candidate_not_alerted_but_registered_alias_is(self):
        self.discover()
        self.post('blog', 'partial', '가상기업아 관련 시황')
        result = self.discover()
        self.assertEqual(result['queued'], 0); self.assertEqual(result['ambiguous'], 1)
        self.post('blog', 'alias', 'DEMO 청약 의견')
        self.assertEqual(self.discover()['queued'], 1)
        self.assertEqual(self.events()[0]['stock'], '가상기업')

    def test_site_only_changes_and_new_catalog_stock_do_not_replay_seen_posts(self):
        self.post('blog', 'other', '새기업 청약 정보')
        self.discover()
        self.stock('가상기업', ['2026-11-18', '2026-11-19'], fields={'공모가': '5,000원'})
        self.stock('새기업', ['2026-11-20', '2026-11-21'])
        result = self.discover()
        self.assertEqual(result['catalog'], 2); self.assertEqual(result['queued'], 0)
        self.assertEqual(self.events(), [])
        self.post('blog', 'other-new', '새기업 청약 추가 설명')
        self.assertEqual(self.discover()['queued'], 1)

    def test_catalog_keeps_only_active_known_current_month_windows(self):
        self.stock('종료기업', ['2026-11-10', '2026-11-11'])
        self.stock('다음달기업', ['2026-12-01', '2026-12-02'])
        self.stock('이미상장', [], extra_events=[{'kind': 'listing', 'date': '2026-11-03', 'raw_label': '상장) 이미상장'}])
        self.stock('KTE', [], fields={'청약일': '12.14 ~ 12.15'})
        self.stock('명시연도기업', [], fields={'청약일': '2026-11-25 ~ 2026-11-26', '청약 증권사': '가상증권'})
        result = monthly_catalog(self.source, self.aliases, TODAY)
        self.assertEqual(set(result), {'가상기업', '명시연도기업'})
        self.assertEqual(result['명시연도기업']['fields']['청약 증권사'], '가상증권')
        self.assertEqual(result['가상기업']['start'], '2026-11-18')
        self.assertEqual(result['가상기업']['end'], '2026-11-19')

    def test_end_day_included_next_day_excluded_and_invalid_window_not_guessed(self):
        self.stock('잘못된날짜', ['2026-11-18', '2026-11-99'])
        self.assertNotIn('잘못된날짜', monthly_catalog(self.source, self.aliases, TODAY))
        self.assertIn('가상기업', monthly_catalog(self.source, self.aliases, date(2026, 11, 19)))
        self.assertNotIn('가상기업', monthly_catalog(self.source, self.aliases, date(2026, 11, 20)))

    def test_month_rollover_refreshes_catalog_without_replaying_previously_seen_items(self):
        self.stock('다음달기업', ['2026-12-02', '2026-12-03'])
        self.post('blog', 'old-nextmonth', '다음달기업 소개')
        self.discover()
        result = self.discover(date(2026, 12, 1))
        self.assertEqual(result['month'], '2026-12'); self.assertEqual(result['catalog'], 1)
        self.assertEqual(result['queued'], 0)
        self.post('video', 'new-nextmonth', '다음달기업 청약 정보')
        self.assertEqual(self.discover(date(2026, 12, 1))['queued'], 1)
        self.assertEqual(self.events()[0]['stock'], '다음달기업')

    def test_cross_month_subscription_window_belongs_to_each_intersected_month(self):
        self.stock('월경계기업', ['2026-11-30', '2026-12-01'])
        self.assertIn('월경계기업', monthly_catalog(self.source, self.aliases, TODAY))
        self.assertIn('월경계기업', monthly_catalog(self.source, self.aliases, date(2026, 12, 1)))
        self.assertNotIn('월경계기업', monthly_catalog(self.source, self.aliases, date(2026, 12, 2)))

    def test_site_failure_does_not_advance_seen_or_catalog_freshness(self):
        self.discover()
        self.post('blog', 'new-after-failure', '가상기업 새 청약 소식')
        self.attempt('error')
        result = self.discover(date(2026, 11, 18))
        self.assertTrue(result['stale_catalog']); self.assertEqual(result['queued'], 0)
        self.assertIsNone(self.setting('catalog_date'))
        self.assertIsNone(self.state.execute("SELECT 1 FROM mention_seen WHERE item_id='new-after-failure'").fetchone())
        self.attempt('success')
        self.assertEqual(self.discover(date(2026, 11, 18))['queued'], 1)

    def test_failed_first_site_fetch_does_not_establish_baseline(self):
        self.attempt('error')
        self.assertTrue(self.discover()['stale_catalog'])
        self.assertIsNone(self.setting('initialized'))
        self.attempt('success')
        result = self.discover()
        self.assertTrue(result['baseline']); self.assertEqual(result['queued'], 0)

    def test_schema_migrates_origin_additively(self):
        self.state.execute('''CREATE TABLE mention_events (
            id INTEGER PRIMARY KEY,event_hash TEXT UNIQUE,stock TEXT,source_id TEXT,source_type TEXT,
            item_id TEXT,snapshot_id INTEGER,input_hash TEXT,input_json TEXT,created_at TEXT,
            analysis_json TEXT,analysis_status TEXT,error_code TEXT,attempts INTEGER)''')
        ensure_schema(self.state)
        columns = {row[1] for row in self.state.execute('PRAGMA table_info(mention_events)')}
        self.assertIn('analysis_origin', columns)
        ensure_schema(self.state)

    def source_attempt(self, source, status):
        self.source.execute("""INSERT INTO source_attempts
            (source_id,started_at,finished_at,status,error,item_count,new_count,changed_count,unchanged_count)
            VALUES (?,'start','finish',?,NULL,0,0,0,0)""", (source, status))
        self.source.commit()

    def test_first_failed_video_source_recovery_baselines_old_videos_before_new_alerts(self):
        self.source.execute("DELETE FROM source_snapshots WHERE source_id='video'")
        self.source.commit()
        self.source_attempt('video', 'error')
        self.assertTrue(self.discover()['baseline'])
        self.assertIsNone(self.state.execute("SELECT 1 FROM mention_source_baselines WHERE source_id='video'").fetchone())
        self.post('video', 'recovered-old-video', '가상기업 과거 영상')
        self.source_attempt('video', 'success')
        result = self.discover()
        self.assertEqual(result['queued'], 0)
        self.assertIsNotNone(self.state.execute("SELECT 1 FROM mention_source_baselines WHERE source_id='video'").fetchone())
        self.post('video', 'new-video-after-recovery', '가상기업 새 의견')
        self.assertEqual(self.discover()['queued'], 1)
        self.assertEqual(self.events()[0]['item_id'], 'new-video-after-recovery')

    def test_successful_empty_source_baselines_without_losing_first_new_item(self):
        self.source.execute("DELETE FROM source_snapshots WHERE source_id='video'")
        self.source.commit()
        self.source_attempt('video', 'success')
        self.discover()
        self.assertIsNotNone(self.state.execute("SELECT 1 FROM mention_source_baselines WHERE source_id='video'").fetchone())
        self.post('video', 'first-new-video', '가상기업 첫 신규 영상')
        self.assertEqual(self.discover()['queued'], 1)

    def test_partial_video_metadata_is_enough_for_initial_source_baseline(self):
        self.source_attempt('video', 'partial')
        self.discover()
        self.post('video', 'partial-new-video', '가상기업 새 영상')
        self.source_attempt('video', 'partial')
        self.assertEqual(self.discover()['queued'], 1)

    def test_same_day_site_failure_invalidates_previous_catalog_date_until_recovery(self):
        self.discover()
        self.assertEqual(self.setting('catalog_date'), '2026-11-17')
        count = self.state.execute('SELECT count(*) FROM mention_catalog').fetchone()[0]
        self.attempt('error')
        result = self.discover()
        self.assertTrue(result['stale_catalog'])
        self.assertIsNone(self.setting('catalog_date'))
        self.assertEqual(self.state.execute('SELECT count(*) FROM mention_catalog').fetchone()[0], count)
        self.attempt('success')
        self.discover()
        self.assertEqual(self.setting('catalog_date'), '2026-11-17')
