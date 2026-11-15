"""Offline security and markup tests for the restricted public-body fetcher."""

import unittest
from unittest.mock import patch

import httpx

from ipo_monitor.body import canonical_url, extract_text, fetch_body
from ipo_monitor.extraction import ExtractionError

URL = "https://blog.naver.com/hycj1025/224471910652"


class BodyTests(unittest.TestCase):
    def response(self, html=b"", status=200, headers=None):
        return httpx.Response(status, content=html,
                              headers=headers or {"content-type": "text/html; charset=utf-8"},
                              request=httpx.Request("GET", URL))

    def test_modern_article_only_and_hidden_content_removed(self):
        html = '<nav>메뉴</nav><div class="x se-main-container"><p>엠비디 청약은 <b>패스</b>한다.</p><script>secret()</script><style>secret</style><p>다음 문장<br>줄</p><img src="anything"></div><footer>댓글</footer>'
        self.assertEqual(extract_text(html), '엠비디 청약은 패스한다. 다음 문장 줄')

    def test_legacy_article_and_entities(self):
        self.assertEqual(extract_text('<div id="postViewArea"><p>A &amp; B</p><div>신청했다.</div></div>outside'), 'A & B 신청했다.')

    def test_missing_empty_or_truncated_container_is_unavailable(self):
        for html in ['<html>로그인이 필요합니다</html>', '<div class="se-main-container"></div>', '<div id="postViewArea"><p>잘린 본문</p>', '<div id="postViewArea"><script>hidden</script></div>']:
            with self.subTest(html=html), self.assertRaisesRegex(ExtractionError, '^body_content_unavailable$'):
                extract_text(html)

    def test_canonical_removes_tracking_and_mobile_form(self):
        self.assertEqual(canonical_url(URL + '?fromRss=true#x'), URL)
        self.assertEqual(canonical_url(URL.replace('blog.naver.com', 'm.blog.naver.com')), URL)

    def test_unsupported_urls_never_make_request(self):
        for url in ['http://blog.naver.com/hycj1025/1', 'https://127.0.0.1/a', 'https://blog.naver.com.evil/hycj1025/1', 'https://blog.naver.com/other/1', 'https://blog.naver.com/hycj1025/1/extra', 'https://user:pass@blog.naver.com/hycj1025/1', 'https://blog.naver.com:443/hycj1025/1', 'https://blog.naver.com/hycj1025/../1']:
            with self.subTest(url=url), patch('ipo_monitor.body.httpx.stream') as stream:
                with self.assertRaisesRegex(ExtractionError, '^body_url_unsupported$'):
                    fetch_body(url)
                stream.assert_not_called()

    def test_fetch_returns_public_text_and_canonical_url(self):
        response = self.response('<div class="se-main-container"><p>참여한다.</p></div>'.encode())
        with patch('ipo_monitor.body.httpx.stream') as stream:
            stream.return_value.__enter__.return_value = response
            self.assertEqual(fetch_body(URL + '?fromRss=true'), (URL, '참여한다.'))
            stream.assert_called_once_with('GET', URL.replace('blog.naver.com', 'm.blog.naver.com'), timeout=20, follow_redirects=False)

    def test_redirects_are_rejected_not_followed(self):
        response = self.response(status=302, headers={'location': 'https://accounts.naver.com/login'})
        with patch('ipo_monitor.body.httpx.stream') as stream:
            stream.return_value.__enter__.return_value = response
            with self.assertRaisesRegex(ExtractionError, '^body_redirect_blocked$'):
                fetch_body(URL)
            self.assertEqual(stream.call_count, 1)

    def test_http_and_network_errors_do_not_leak_exception_data(self):
        with patch('ipo_monitor.body.httpx.stream') as stream:
            stream.return_value.__enter__.return_value = self.response(status=403)
            with self.assertRaisesRegex(ExtractionError, '^body_http_403$'):
                fetch_body(URL)
        with patch('ipo_monitor.body.httpx.stream', side_effect=httpx.ReadTimeout('secret must not leak')):
            with self.assertRaisesRegex(ExtractionError, '^body_network_error$'):
                fetch_body(URL)

    def test_content_type_and_response_size_are_bounded(self):
        for response, code in [(self.response(headers={'content-type':'application/json'}), 'body_content_type_invalid'), (self.response(b'x' * 50), 'body_too_large')]:
            with patch('ipo_monitor.body.httpx.stream') as stream, patch('ipo_monitor.body.MAX_BYTES', 20):
                stream.return_value.__enter__.return_value = response
                with self.assertRaisesRegex(ExtractionError, '^' + code + '$'):
                    fetch_body(URL)


if __name__ == '__main__':
    unittest.main()
