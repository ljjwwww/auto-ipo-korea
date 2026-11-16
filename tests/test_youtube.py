"""Offline channel-feed, subtitle-process and VTT regression tests."""

from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import httpx

from ipo_monitor import CollectionError
from ipo_monitor.youtube import collect, parse_vtt

CHANNEL = 'UC' + 'a' * 22
VIDEO = 'Abcdefg1234'
SOURCE = {'id': 'test', 'type': 'youtube', 'name': 'test', 'channel_id': CHANNEL,
          'url': 'https://www.youtube.com/feeds/videos.xml?channel_id=' + CHANNEL}
VTT = '''WEBVTT
Kind: captions
Language: ko

00:00:00.000 --> 00:00:01.000
가상기업<00:00:00.200><c> 공모주</c>

00:00:01.000 --> 00:00:02.000
가상기업 공모주
청약을 검토한다.

00:00:02.000 --> 00:00:03.000
청약을 검토한다.

00:00:05.000 --> 00:00:06.000
청약을 검토한다.
'''


def feed(title='test', channel=CHANNEL, extra=''):
    return f'''<feed xmlns="http://www.w3.org/2005/Atom"
    xmlns:yt="http://www.youtube.com/xml/schemas/2015"
    xmlns:media="http://search.yahoo.com/mrss/">
    <yt:channelId>{channel}</yt:channelId>
    <entry><yt:videoId>{VIDEO}</yt:videoId><yt:channelId>{channel}</yt:channelId>
    <title>{title}</title><published>2026-11-15T10:00:00+09:00</published>
    <updated>2026-11-15T10:00:00+09:00</updated>
    <media:group><media:description>공모주 설명</media:description></media:group></entry>
    {extra}</feed>'''.encode()


class VttTests(unittest.TestCase):
    def test_rolling_overlap_removed_without_global_dedup(self):
        rows = parse_vtt(VTT)
        self.assertEqual([r['text'] for r in rows], ['가상기업 공모주', '청약을 검토한다.', '청약을 검토한다.'])
        self.assertEqual(rows[1]['start'], '00:00:01.000')
        self.assertEqual(rows[-1]['end'], '00:00:06.000')

    def test_bom_crlf_entities_and_cue_identifier(self):
        rows = parse_vtt('\ufeffWEBVTT\r\n\r\nfirst\r\n00:01.000 --> 00:02.000 align:start\r\nA &amp; B\r\n')
        self.assertEqual(rows, [{'start': '00:01.000', 'end': '00:02.000', 'text': 'A & B'}])

    def test_malformed_empty_or_other_format_is_rejected(self):
        for text in (None, 'WEBVTT\n\n00:00:00.000 --> \ntext', 'not VTT', 'WEBVTT\n\n', 'WEBVTT\n\nBAD\ntext',
                     'WEBVTT\n\n00:00:02.000 --> 00:00:01.000\ntext',
                     'WEBVTT\n\n00:60:00.000 --> 00:61:00.000\ntext'):
            with self.subTest(text=text), self.assertRaises(CollectionError):
                parse_vtt(text)

    def test_space_only_payload_line_from_live_youtube(self):
        text='WEBVTT\nKind: captions\nLanguage: ko\n\n00:00:00.040 --> 00:00:01.630 align:start position:0%\n \n가상기업<00:00:00.199><c> 공모주</c>\n\n'
        self.assertEqual(parse_vtt(text)[0]['text'],'가상기업 공모주')

    def test_comment_and_style_blocks_not_treated_as_speech(self):
        rows = parse_vtt('WEBVTT\n\nNOTE example\ncomment\n\nSTYLE\n::cue {}\n\n00:00:00.000 --> 00:00:01.000\n발언\n')
        self.assertEqual(rows[0]['text'], '발언')


class YoutubeTests(unittest.TestCase):
    def response(self, content=None, status=200, headers=None):
        return httpx.Response(status, content=feed() if content is None else content,
                              request=httpx.Request('GET', SOURCE['url']), headers=headers)

    def call(self, source=None, previous=None, response=None):
        with patch('ipo_monitor.youtube.httpx.stream') as stream:
            stream.return_value.__enter__.return_value = response or self.response()
            return collect(SOURCE if source is None else source, previous or {})

    def downloader(self, argv, **kwargs):
        self.command = argv
        self.process_options = kwargs
        template = argv[argv.index('-o') + 1]
        Path(template.replace('%(id)s', VIDEO).replace('%(ext)s', 'ko-orig.vtt')).write_text(VTT)
        return subprocess.CompletedProcess(argv, 0)

    def test_metadata_captions_and_no_media_download(self):
        with patch('ipo_monitor.youtube.shutil.which', return_value='/mock/yt-dlp'), patch(
                'ipo_monitor.youtube.subprocess.run', side_effect=self.downloader):
            rows = self.call()
        item = rows[0]
        self.assertEqual(item['item_id'], VIDEO)
        self.assertEqual(item['url'], 'https://www.youtube.com/watch?v=' + VIDEO)
        self.assertEqual(item['published_at'], '2026-11-15T10:00:00+09:00')
        self.assertEqual(item['payload']['summary'], '공모주 설명')
        captions = item['payload']['captions']
        self.assertEqual(captions['status'], 'available')
        self.assertEqual(captions['language'], 'ko-orig')
        self.assertEqual(len(captions['segments']), 3)
        self.assertIn('--skip-download', self.command)
        self.assertIn('--ignore-config', self.command)
        self.assertIn('--no-playlist', self.command)
        self.assertIn('--retries', self.command)
        self.assertNotIn('--cookies', self.command)
        self.assertNotIn('--write-info-json', self.command)
        self.assertNotIn('shell', self.process_options)
        self.assertEqual(self.process_options['timeout'], 90)

    def test_available_unchanged_captions_reused_and_metadata_change_refetched(self):
        with patch('ipo_monitor.youtube._captions', return_value={'status': 'available', 'metadata_hash': 'initial'}) as captions:
            first = self.call()
        # Real downloader normally returns the actual metadata hash: obtain it from call args.
        first[0]['payload']['captions']['metadata_hash'] = captions.call_args.args[1]
        previous = {VIDEO: first[0]['payload']}
        with patch('ipo_monitor.youtube._captions') as captions:
            second = self.call(previous=previous)
            captions.assert_not_called()
            self.assertEqual(first, second)
        with patch('ipo_monitor.youtube._captions', return_value={'status': 'missing'}) as captions:
            self.call(previous=previous, response=self.response(feed('changed title')))
            captions.assert_called_once()

    def test_explicit_refresh_refetches(self):
        with patch('ipo_monitor.youtube._captions', return_value={'status':'available'}) as captions:
            first = self.call()
        first[0]['payload']['captions']['metadata_hash'] = captions.call_args.args[1]
        with patch('ipo_monitor.youtube._captions', return_value={'status':'missing'}) as captions:
            self.call(source={**SOURCE, 'refresh_subtitles': True}, previous={VIDEO:first[0]['payload']})
            captions.assert_called_once()

    def test_missing_or_error_subtitles_are_retried(self):
        for status in ('missing', 'error'):
            with self.subTest(status=status), patch('ipo_monitor.youtube._captions', return_value={'status':'missing'}) as captions:
                self.call(previous={VIDEO:{'captions':{'status':status}}})
                captions.assert_called_once()

    def test_no_file_is_missing_but_timeout_and_process_failure_are_errors(self):
        cases = [(subprocess.CompletedProcess([],0),None,'missing',None),
                 (subprocess.CompletedProcess([],1),None,'error','youtube_subtitle_request_failed'),
                 (None,subprocess.TimeoutExpired('secret',90),'error','youtube_subtitle_timeout')]
        for completed,error,status,code in cases:
            with self.subTest(status=status,code=code), patch('ipo_monitor.youtube.shutil.which',return_value='/mock/yt-dlp'), patch(
                    'ipo_monitor.youtube.subprocess.run',return_value=completed,side_effect=error):
                item=self.call()[0]
                self.assertEqual(item['payload']['captions']['status'],status)
                self.assertEqual(item['payload']['captions'].get('error'),code)
                self.assertNotIn('secret',str(item))

    def test_uvx_fallback_and_missing_downloader(self):
        def which(command):
            return '/mock/uvx' if command=='uvx' else None
        with patch('ipo_monitor.youtube.shutil.which', side_effect=which), patch(
                'ipo_monitor.youtube.subprocess.run',side_effect=self.downloader):
            self.assertEqual(self.call()[0]['payload']['captions']['status'],'available')
            self.assertEqual(self.command[:2],['/mock/uvx','yt-dlp'])
        with patch('ipo_monitor.youtube.shutil.which',return_value=None):
            self.assertEqual(self.call()[0]['payload']['captions']['error'],'youtube_downloader_missing')

    def test_invalid_or_non_vtt_subtitle_is_error_not_success(self):
        def invalid(argv,**kwargs):
            template=argv[argv.index('-o')+1]
            Path(template.replace('%(id)s',VIDEO).replace('%(ext)s','ko-orig.vtt')).write_text('bad')
            return subprocess.CompletedProcess(argv,0)
        with patch('ipo_monitor.youtube.shutil.which',return_value='/mock/yt-dlp'),patch(
                'ipo_monitor.youtube.subprocess.run',side_effect=invalid):
            self.assertEqual(self.call()[0]['payload']['captions']['error'],'youtube_subtitle_invalid')

    def test_channel_and_feed_url_are_restricted_before_network(self):
        for source in ({**SOURCE,'channel_id':'invalid'}, {**SOURCE,'url':'https://evil.example/feed'},
                       {**SOURCE,'url':SOURCE['url']+'&secret=value'}, {**SOURCE,'max_videos':True}):
            with self.subTest(source=source),patch('ipo_monitor.youtube.httpx.stream') as stream:
                with self.assertRaises(CollectionError):
                    collect(source,{})
                stream.assert_not_called()

    def test_malformed_or_mismatched_feed_does_not_download(self):
        for content in (b'<feed>', feed(channel='UC'+'b'*22),feed().replace(VIDEO.encode(),b'bad')):
            with self.subTest(content=content),patch('ipo_monitor.youtube._captions') as captions:
                with self.assertRaises(CollectionError):self.call(response=self.response(content))
                captions.assert_not_called()

    def test_redirects_http_errors_and_network_errors_are_safe(self):
        for response in (self.response(status=302,headers={'location':'https://evil.example'}),self.response(status=503)):
            with self.assertRaises(CollectionError):self.call(response=response)
        with patch('ipo_monitor.youtube.httpx.stream',side_effect=httpx.ConnectError('secret')):
            with self.assertRaisesRegex(CollectionError,'^youtube_feed_network_error$'):
                collect(SOURCE,{})

    def test_caption_korean_fallback_and_size_limit(self):
        def write_korean(argv, **kwargs):
            template = argv[argv.index('-o') + 1]
            Path(template.replace('%(id)s', VIDEO).replace('%(ext)s', 'ko.vtt')).write_text(VTT)
            return subprocess.CompletedProcess(argv, 0)
        with patch('ipo_monitor.youtube.shutil.which', return_value='/mock/yt-dlp'), patch(
                'ipo_monitor.youtube.subprocess.run', side_effect=write_korean):
            self.assertEqual(self.call()[0]['payload']['captions']['language'], 'ko')
        with patch('ipo_monitor.youtube.shutil.which', return_value='/mock/yt-dlp'), patch(
                'ipo_monitor.youtube.subprocess.run', side_effect=write_korean), patch(
                'ipo_monitor.youtube.CAPTION_LIMIT', 10):
            self.assertEqual(self.call()[0]['payload']['captions']['error'], 'youtube_subtitle_too_large')

    def test_window_limit_and_duplicate_video_id_do_not_repeat_requests(self):
        entry = feed().decode().split('<entry>', 1)[1].split('</entry>', 1)[0]
        second = '<entry>' + entry.replace(VIDEO, 'Zbcdefg1234') + '</entry>'
        duplicate = '<entry>' + entry + '</entry>'
        response = self.response(feed(extra=duplicate + second))
        with patch('ipo_monitor.youtube._captions', return_value={'status':'missing'}) as captions:
            rows = self.call(source={**SOURCE, 'max_videos':1},response=response)
            self.assertEqual(len(rows),1)
            self.assertEqual(captions.call_count,1)
        with patch('ipo_monitor.youtube._captions', return_value={'status':'missing'}) as captions:
            rows = self.call(response=response)
            self.assertEqual(len(rows),2)
            self.assertEqual(captions.call_count,2)

    def test_feed_size_bounded(self):
        with patch('ipo_monitor.youtube.FEED_LIMIT',10):
            with self.assertRaisesRegex(CollectionError,'youtube_feed_too_large'):
                self.call()

    def test_live_feed_channel_id_without_uc_prefix(self):
        content=feed().replace(CHANNEL.encode(), CHANNEL[2:].encode())
        with patch('ipo_monitor.youtube._captions',return_value={'status':'missing'}):
            rows=self.call(response=self.response(content))
        self.assertEqual(rows[0]['payload']['channel_id'],CHANNEL)

    def test_empty_valid_feed_is_success_without_subtitle_requests(self):
        content=f'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015"><yt:channelId>{CHANNEL}</yt:channelId></feed>'.encode()
        with patch('ipo_monitor.youtube._captions') as captions:
            self.assertEqual(self.call(response=self.response(content)),[])
            captions.assert_not_called()


if __name__ == '__main__':
    unittest.main()
