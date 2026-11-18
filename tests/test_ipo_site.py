"""Synthetic public-page layout tests; no actual third-party content is committed."""
import unittest
from contextlib import nullcontext
from unittest.mock import patch

import httpx

from ipo_monitor import CollectionError
from ipo_monitor.ipo_site import SITE_URL, collect, parse_site


def cell(day, labels):
    return (f'<button role="gridcell" aria-label="2026년 11월 {day}일 일정">'
            + ''.join(f'<span data-category="ipo" data-tone="{kind}">{label}</span>'
                      for kind, label in labels) + '</button>')


def page(events='', tables=''):
    return '<html><div role="grid">' + events + '</div>' + tables + '</html>'


def table(name='가상기업', value='미정'):
    return ('<section aria-label="청약예정 · 수요예측"><h2>청약예정 · 수요예측</h2><table>'
            '<thead><tr><th>종목명</th><th>수요예측 경쟁률</th><th>공모가</th></tr></thead>'
            f'<tbody><tr><th scope="row"><strong>{name}</strong><span>코스닥 · 가상 설명</span></th>'
            f'<td>{value}</td><td>1,000원<span>희망</span></td></tr></tbody></table></section>')


def changed_header_table(headers=('기업명', '공모가')):
    cells = ''.join(f'<th>{header}</th>' for header in headers)
    values = ''.join('<td>미정</td>' for _ in headers[1:])
    return ('<section aria-label="청약예정 · 수요예측"><h2>청약예정 · 수요예측</h2><table>'
            f'<thead><tr>{cells}</tr></thead><tbody><tr><th scope="row"><strong>가상기업</strong></th>'
            f'{values}</tr></tbody></table></section>')


def unrelated_table():
    return ('<section aria-label="공지사항"><h2>공지사항</h2><table>'
            '<thead><tr><th>제목</th><th>작성일</th></tr></thead>'
            '<tbody><tr><td>안내</td><td>2026-11-18</td></tr></tbody></table></section>')


class SiteParserTests(unittest.TestCase):
    def test_groups_events_and_table_without_rating_inference(self):
        html = page(cell(18, [('subscription', '청약) 가상기업⭐⭐')])
                    + cell(19, [('subscription', '청약) 가상기업⭐⭐')])
                    + cell(27, [('listing', '상장) 가상기업⭐⭐')]), table())
        rows = parse_site(html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['item_id'], '가상기업')
        payload = rows[0]['payload']
        self.assertEqual([e['date'] for e in payload['events']], ['2026-11-18', '2026-11-19', '2026-11-27'])
        self.assertEqual(payload['events'][0]['stars'], 2)
        self.assertEqual(payload['table_rows'][0]['fields']['수요예측 경쟁률'], '미정')
        self.assertEqual(payload['table_rows'][0]['fields']['공모가'], '1,000원 희망')
        self.assertNotIn('opinion', payload)

    def test_absent_and_cross_are_distinct_and_not_zero(self):
        rows = parse_site(page(cell(18, [('subscription', '청약) 가상무별'), ('subscription', '청약) 가상패스❌')])))
        values = {row['title']: row['payload']['events'][0] for row in rows}
        self.assertEqual(values['가상무별']['raw_marker'], '')
        self.assertIsNone(values['가상무별']['stars'])
        self.assertEqual(values['가상패스']['raw_marker'], '❌')
        self.assertIsNone(values['가상패스']['stars'])

    def test_parenthetical_name_is_preserved(self):
        row = parse_site(page(cell(3, [('subscription', '청약) 가상기업(환)⭐')])))[0]
        self.assertEqual(row['title'], '가상기업(환)')

    def test_table_only_company_preserves_description_and_fields(self):
        row = parse_site(page('', table()))[0]
        self.assertEqual(row['title'], '가상기업')
        self.assertEqual(row['payload']['events'], [])
        self.assertIn('코스닥', row['payload']['table_rows'][0]['name_cell'])

    def test_hidden_markup_is_not_read_as_event_or_text(self):
        html = page(cell(18, [('subscription', '청약) 가상기업<script>비밀</script>⭐⭐')]))
        row = parse_site(html)[0]
        self.assertEqual(row['title'], '가상기업')

    def test_bad_date_or_truncated_or_non_page_are_errors(self):
        for html in ('<html>로그인</html>', '<div role="grid">', page('<button role="gridcell"><span data-category="ipo" data-tone="subscription">청약) 가상기업</span></button>'), page('', table().replace('</table>', ''))):
            with self.subTest(html=html), self.assertRaises(CollectionError):
                parse_site(html)

    def test_bad_table_column_count_is_error(self):
        with self.assertRaises(CollectionError):
            parse_site(page('', table().replace('<td>미정</td>', '')))

    def test_empty_requires_explicit_message(self):
        self.assertEqual(parse_site(page('일정이 없습니다.')), [])
        with self.assertRaises(CollectionError):
            parse_site(page())

    def test_unrelated_empty_section_does_not_mask_changed_calendar_markup(self):
        html = page('<button role="gridcell" aria-label="2026년 11월 18일 일정"><span>청약) 가상기업</span></button>',
                    '<section>지금은 청약진행중 종목이 없어요.</section>')
        with self.assertRaises(CollectionError):
            parse_site(html)

    def test_raw_marker_change_is_visible_in_record(self):
        old = parse_site(page(cell(18, [('subscription', '청약) 가상기업')])))[0]
        new = parse_site(page(cell(18, [('subscription', '청약) 가상기업⭐⭐')])))[0]
        self.assertEqual(old['item_id'], new['item_id'])
        self.assertNotEqual(old['payload'], new['payload'])

    def test_changed_header_in_recognized_ipo_section_is_error(self):
        html = page(cell(18, [('subscription', '청약) 가상기업⭐')]), changed_header_table())
        with self.assertRaises(CollectionError) as raised:
            parse_site(html)
        self.assertIn('머리글', str(raised.exception))

    def test_unrelated_table_header_change_does_not_fail(self):
        html = page(cell(18, [('subscription', '청약) 가상기업⭐')]), unrelated_table())
        rows = parse_site(html)
        self.assertEqual([row['item_id'] for row in rows], ['가상기업'])

    def test_mixed_star_and_cross_is_conflict_not_stars(self):
        event = parse_site(page(cell(18, [('subscription', '청약) 가상기업⭐❌')])))[0]['payload']['events'][0]
        self.assertEqual(event['raw_marker'], '⭐❌')
        self.assertIsNone(event['stars'])
        self.assertTrue(event['marker_conflict'])

    def test_mixed_marker_in_table_is_conflict_not_stars(self):
        observation = parse_site(page('', table(name='가상기업❌⭐')))[0]['payload']['rating_observations'][0]
        self.assertEqual(observation['raw_marker'], '❌⭐')
        self.assertIsNone(observation['stars'])
        self.assertTrue(observation['marker_conflict'])


class SiteFetchTests(unittest.TestCase):
    def response(self, content, status=200, headers=None):
        return httpx.Response(status, content=content, headers=headers or {'content-type': 'text/html; charset=utf-8'}, request=httpx.Request('GET', SITE_URL))

    def test_fetch_and_previous_are_not_mutated(self):
        previous = {'sample': {'anything': 'keep'}}
        response = self.response(page(cell(18, [('subscription', '청약) 가상기업⭐')])).encode())
        with patch('ipo_monitor.ipo_site.httpx.stream', return_value=nullcontext(response)) as request:
            rows = collect({'url': SITE_URL}, previous)
        self.assertEqual(rows[0]['url'], SITE_URL)
        self.assertEqual(previous, {'sample': {'anything': 'keep'}})
        self.assertFalse(request.call_args.kwargs['follow_redirects'])

    def test_unsupported_url_never_requests(self):
        with patch('ipo_monitor.ipo_site.httpx.stream') as request, self.assertRaises(CollectionError):
            collect({'url': 'https://example.com'}, {})
        request.assert_not_called()

    def test_redirect_http_and_type_are_errors(self):
        for response in (self.response(b'', 302, {'location': 'https://example.com'}), self.response(b'secret', 503), self.response(b'{}', headers={'content-type': 'application/json'})):
            with self.subTest(status=response.status_code), patch('ipo_monitor.ipo_site.httpx.stream', return_value=nullcontext(response)), self.assertRaises(CollectionError):
                collect({'url': SITE_URL}, {})

    def test_network_error_is_static(self):
        with patch('ipo_monitor.ipo_site.httpx.stream', side_effect=httpx.ConnectError('secret')), self.assertRaises(CollectionError) as raised:
            collect({'url': SITE_URL}, {})
        self.assertNotIn('secret', str(raised.exception))

    def test_size_limit(self):
        response = self.response(b'x' * 50)
        with patch('ipo_monitor.ipo_site.MAX_BYTES', 10), patch('ipo_monitor.ipo_site.httpx.stream', return_value=nullcontext(response)), self.assertRaises(CollectionError):
            collect({'url': SITE_URL}, {})
