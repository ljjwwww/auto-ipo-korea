"""Offline CLI acceptance tests; every test owns its feed and database."""

from contextlib import nullcontext, redirect_stderr, redirect_stdout
import io
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from xml.sax.saxutils import escape


def item(number, *, guid=True, date="Sat, 14 Nov 2026 01:00:00 +0000"):
    identity = f'<guid isPermaLink="false">post-{number}</guid>' if guid else ""
    return (
        f"<item><title>후보 {number}</title>{identity}"
        f"<link>https://example.invalid/posts/{number}</link>"
        f"<pubDate>{escape(date)}</pubDate>"
        f"<description>분석 {number}</description></item>"
    )


def rss(*entries):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>샘플</title>'
        '<link>https://example.invalid/</link><description>샘플 피드</description>'
        + "".join(entries)
        + "</channel></rss>"
    )


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.feed = self.root / "sample.xml"
        self.db = self.root / "posts.sqlite3"

    def write_feed(self, *entries, path=None):
        target = path or self.feed
        target.write_text(rss(*entries), encoding="utf-8")
        return target

    def run_cli(self, feed=None):
        return subprocess.run(
            [sys.executable, "-m", "ipo_monitor", "--feed", str(feed or self.feed),
             "--db", str(self.db)],
            capture_output=True, text=True, timeout=15,
        )

    def success(self, result, expected):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(expected, result.stdout)

    def rows(self, columns="*"):
        with sqlite3.connect(self.db) as connection:
            return connection.execute(f"SELECT {columns} FROM posts").fetchall()

    def test_initial_feed_is_saved_and_displayed(self):
        self.write_feed(item(1), item(2))
        result = self.run_cli()
        self.success(result, "기준 목록: 2건")
        self.assertIn("후보 1", result.stdout)
        self.assertIn("https://example.invalid/posts/2", result.stdout)
        self.assertEqual(len(self.rows()), 2)
        self.assertIn(("후보 1", "분석 1"), self.rows("title, summary"))

    def test_rss_without_channel_is_not_a_valid_empty_feed(self):
        self.feed.write_text('<rss version="2.0"/>', encoding="utf-8")
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("구조 오류", result.stderr)
        self.assertFalse(self.db.exists())

    def test_repeat_feed_reports_zero_and_does_not_repeat_titles(self):
        self.write_feed(item(1), item(2))
        self.success(self.run_cli(), "기준 목록: 2건")
        result = self.run_cli()
        self.success(result, "새 글: 0건")
        self.assertNotIn("후보 1", result.stdout)
        self.assertEqual(len(self.rows()), 2)

    def test_one_appended_post_is_the_only_new_result(self):
        self.write_feed(item(1), item(2))
        self.success(self.run_cli(), "기준 목록: 2건")
        self.write_feed(item(1), item(2), item(3))
        result = self.run_cli()
        self.success(result, "새 글: 1건")
        self.assertIn("후보 3", result.stdout)
        self.assertNotIn("후보 1", result.stdout)
        self.assertEqual(len(self.rows()), 3)

    def test_same_id_from_another_feed_is_not_suppressed(self):
        self.write_feed(item(1))
        self.success(self.run_cli(), "기준 목록: 1건")
        other = self.write_feed(item(1), path=self.root / "other.xml")
        self.success(self.run_cli(other), "기준 목록: 1건")
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(len(set(self.rows("feed_id"))), 2)

    def test_missing_guid_uses_link_for_deduplication(self):
        self.write_feed(item(1, guid=False))
        self.success(self.run_cli(), "기준 목록: 1건")
        self.success(self.run_cli(), "새 글: 0건")
        self.assertEqual(len(self.rows()), 1)

    def test_guid_remains_identity_when_link_changes(self):
        self.write_feed(item(1))
        self.success(self.run_cli(), "기준 목록: 1건")
        self.write_feed(item(1).replace("posts/1", "posts/changed"))
        self.success(self.run_cli(), "새 글: 0건")
        self.assertEqual(len(self.rows()), 1)

    def test_malformed_feed_does_not_commit_partial_entries(self):
        self.write_feed(item(1))
        self.success(self.run_cli(), "기준 목록: 1건")
        self.feed.write_text(rss(item(1), item(2))[:-8], encoding="utf-8")
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stderr.strip())
        self.assertNotIn("새 글:", result.stdout)
        self.assertEqual(len(self.rows()), 1)

    def test_non_feed_xml_is_an_error_not_an_empty_success(self):
        self.feed.write_text("<html><body>not a feed</body></html>", encoding="utf-8")
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stderr.strip())
        self.assertNotIn("기준 목록:", result.stdout)

    def test_empty_valid_feed_marks_baseline_and_next_post_is_new(self):
        self.write_feed()
        self.success(self.run_cli(), "기준 목록: 0건")
        self.success(self.run_cli(), "새 글: 0건")
        self.write_feed(item(1))
        self.success(self.run_cli(), "새 글: 1건")

    def test_duplicate_ids_within_feed_are_displayed_once(self):
        self.write_feed(item(1), item(1))
        self.success(self.run_cli(), "기준 목록: 1건")
        self.assertEqual(len(self.rows()), 1)

    def test_invalid_entry_does_not_save_other_valid_entries(self):
        self.write_feed(item(1), "<item><title>원문 누락</title></item>")
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stderr.strip())
        if self.db.exists():
            self.assertEqual(len(self.rows()), 0)

    def test_rfc_unknown_offset_is_not_assumed_utc(self):
        raw = "Sat, 14 Nov 2026 01:00:00 -0000"
        self.write_feed(item(1, date=raw))
        result = self.run_cli()
        self.success(result, "기준 목록: 1건")
        self.assertIn("시간대 미확인", result.stdout)
        self.assertEqual(self.rows("published_raw, published_at"), [(raw, None)])

    def test_missing_local_file_is_reported_as_error(self):
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stderr.strip())
        self.assertNotIn("기준 목록:", result.stdout)

    def test_oversized_local_feed_is_rejected_before_database_creation(self):
        self.feed.write_bytes(b" " * (2 * 1024 * 1024 + 1))
        result = self.run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("크기 제한", result.stderr)
        self.assertFalse(self.db.exists())

    def test_dtd_is_rejected_before_xml_entity_expansion(self):
        self.feed.write_text(
            rss(item(1)).replace("?>", '?><!DOCTYPE rss [<!ENTITY name "기업">]>', 1)
            .replace("후보 1", "&name;"),
            encoding="utf-8",
        )
        result = self.run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("DTD", result.stderr)
        self.assertFalse(self.db.exists())

    def test_known_timezone_is_displayed_in_seoul(self):
        self.write_feed(item(1))
        result = self.run_cli()
        self.success(result, "기준 목록: 1건")
        self.assertIn("10:00:00", result.stdout)
        self.assertIn("+09:00", result.stdout)
        self.assertIsNotNone(self.rows("published_at")[0][0])

    def test_unknown_timezone_is_preserved_without_guessing(self):
        raw = "Sat, 14 Nov 2026 01:00:00"
        self.write_feed(item(1, date=raw))
        result = self.run_cli()
        self.success(result, "기준 목록: 1건")
        self.assertIn("시간대 미확인", result.stdout)
        self.assertEqual(self.rows("published_raw, published_at"), [(raw, None)])


class HttpTests(unittest.TestCase):
    def call_with_response(self, response=None, error=None):
        import ipo_monitor

        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "posts.sqlite3"
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch("ipo_monitor.httpx.stream", return_value=nullcontext(response), side_effect=error):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = ipo_monitor.main([
                        "--feed", "https://example.invalid/rss", "--db", str(db)])
            return code, stdout.getvalue(), stderr.getvalue(), db.exists()

    def test_network_timeout_is_an_error_without_database_creation(self):
        import httpx

        code, stdout, stderr, exists = self.call_with_response(
            error=httpx.ConnectTimeout("secret must not leak"))
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("네트워크 오류", stderr)
        self.assertNotIn("secret", stderr)
        self.assertFalse(exists)

    def test_http_error_is_distinct_from_empty_feed(self):
        import httpx

        response = httpx.Response(503, request=httpx.Request("GET", "https://example.invalid/rss"))
        code, stdout, stderr, exists = self.call_with_response(response=response)
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("503", stderr)
        self.assertFalse(exists)

    def test_http_success_uses_same_collection_path(self):
        import httpx

        response = httpx.Response(
            200, content=rss(item(1)).encode(),
            request=httpx.Request("GET", "https://example.invalid/rss"))
        code, stdout, stderr, exists = self.call_with_response(response=response)
        self.assertEqual(code, 0, stderr)
        self.assertIn("기준 목록: 1건", stdout)
        self.assertTrue(exists)

    def test_oversized_http_response_is_rejected_without_database(self):
        import httpx

        response = httpx.Response(
            200, content=b" " * (2 * 1024 * 1024 + 1),
            request=httpx.Request("GET", "https://example.invalid/rss"))
        code, stdout, stderr, exists = self.call_with_response(response=response)
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("크기 제한", stderr)
        self.assertFalse(exists)


if __name__ == "__main__":
    unittest.main()
