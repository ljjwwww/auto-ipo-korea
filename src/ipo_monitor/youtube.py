"""Channel metadata and Korean subtitles only; no media or AI downloads."""

from datetime import datetime, timedelta
from hashlib import sha256
import html
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit, parse_qs
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx

from . import CollectionError, published_time

FEED_LIMIT = 2 * 1024 * 1024
CAPTION_LIMIT = 8 * 1024 * 1024
DEFAULT_SUBTITLE_REFRESH_HOURS = 24
MAX_SUBTITLE_REFRESH_HOURS = 24 * 365
ATOM = '{http://www.w3.org/2005/Atom}'
YT = '{http://www.youtube.com/xml/schemas/2015}'
MEDIA = '{http://search.yahoo.com/mrss/}'
_TIMESTAMP = re.compile(r'(?:(\d{2,}):)?(\d{2}):(\d{2})\.(\d{3})')


def _now():
    return datetime.now(ZoneInfo('Asia/Seoul'))


def _subtitle_due(cached, metadata_hash, hours):
    """Refresh only after the interval; volatile check time never enters hashes."""
    if not isinstance(cached, dict) or cached.get('status') != 'available':
        return True
    if cached.get('metadata_hash') != metadata_hash:
        return True
    stamp = cached.get('last_checked_at')
    if not isinstance(stamp, str):
        return True
    try:
        checked = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    if checked.tzinfo is None:
        return True
    return _now() - checked >= timedelta(hours=hours)


def _timestamp(value):
    match = _TIMESTAMP.fullmatch(value)
    if not match:
        raise CollectionError('youtube_vtt_timestamp_invalid')
    hours, minutes, seconds, millis = (int(n or 0) for n in match.groups())
    if minutes >= 60 or seconds >= 60:
        raise CollectionError('youtube_vtt_timestamp_invalid')
    return (hours * 3600 + minutes * 60 + seconds) * 1000 + millis


def parse_vtt(text: str) -> list[dict]:
    """Keep cue times, remove markup and adjacent rolling-caption overlap.

    This removes only overlapping/near-contiguous repeated lines, not repeated
    phrases later in the video. Word-level timings are not reconstructed.
    """
    if not isinstance(text, str):
        raise CollectionError('youtube_vtt_header_invalid')
    text = text.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
    if not text.splitlines() or not re.fullmatch(r'WEBVTT(?:[ \t].*)?', text.splitlines()[0]):
        raise CollectionError('youtube_vtt_header_invalid')
    segments = []
    previous_lines = []
    previous_end = -1
    # YouTube emits a space-only payload line before the first spoken line.
    # Treating that as a block separator creates an orphaned speech block.
    for block in re.split(r'\n{2,}', text):
        lines = block.splitlines()
        if not block.strip() or lines[0].startswith(('WEBVTT', 'NOTE', 'STYLE', 'REGION')):
            continue
        timing = next((i for i, line in enumerate(lines) if ' --> ' in line), None)
        if timing is None:
            raise CollectionError('youtube_vtt_cue_invalid')
        parts = lines[timing].split(' --> ', 1)
        end_parts = parts[1].split()
        if not end_parts:
            raise CollectionError('youtube_vtt_timestamp_invalid')
        start, end = parts[0].strip(), end_parts[0]
        start_ms, end_ms = _timestamp(start), _timestamp(end)
        if end_ms <= start_ms:
            raise CollectionError('youtube_vtt_timestamp_invalid')
        readable = []
        for line in lines[timing + 1:]:
            value = ' '.join(html.unescape(re.sub(r'<[^>]*>', '', line)).split())
            if value:
                readable.append(value)
        overlap = 0
        if previous_end >= 0 and start_ms <= previous_end + 50:
            for count in range(1, min(len(previous_lines), len(readable)) + 1):
                if previous_lines[-count:] == readable[:count]:
                    overlap = count
        new_lines = readable[overlap:]
        if new_lines:
            segments.append({'start': start, 'end': end, 'text': ' '.join(new_lines)})
        previous_lines, previous_end = readable, end_ms
    if not segments:
        raise CollectionError('youtube_vtt_empty')
    return segments


def _feed(source):
    channel_id = source.get('channel_id', '')
    if not isinstance(channel_id, str) or not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', channel_id):
        raise CollectionError('youtube_channel_id_invalid')
    expected = 'https://www.youtube.com/feeds/videos.xml?channel_id=' + channel_id
    configured = source.get('url', expected)
    try:
        parts = urlsplit(configured)
    except (ValueError, TypeError):
        raise CollectionError('youtube_feed_url_invalid') from None
    if (parts.scheme != 'https' or parts.netloc != 'www.youtube.com'
            or parts.path not in {'/feeds/videos.xml', '/xml/feeds/videos.xml'}
            or parse_qs(parts.query) != {'channel_id': [channel_id]} or parts.fragment):
        raise CollectionError('youtube_feed_url_invalid')
    try:
        with httpx.stream('GET', expected, timeout=20, follow_redirects=False) as response:
            if response.is_redirect:
                raise CollectionError('youtube_feed_redirect_blocked')
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                size += len(chunk)
                if size > FEED_LIMIT:
                    raise CollectionError('youtube_feed_too_large')
                chunks.append(chunk)
            return b''.join(chunks)
    except httpx.HTTPStatusError as exc:
        raise CollectionError(f'youtube_feed_http_{exc.response.status_code}') from None
    except httpx.RequestError:
        raise CollectionError('youtube_feed_network_error') from None


def _captions(video_id, metadata_hash):
    base = {'status': 'missing', 'language': None, 'segments': [], 'text': '',
            'metadata_hash': metadata_hash}
    executable = shutil.which('yt-dlp')
    if executable:
        command = [executable]
    else:
        uvx = shutil.which('uvx')
        if not uvx:
            return {**base, 'status': 'error', 'error': 'youtube_downloader_missing'}
        command = [uvx, 'yt-dlp']
    try:
        with tempfile.TemporaryDirectory(prefix='ipo-youtube-') as directory:
            command += [
                '--ignore-config', '--no-playlist', '--no-simulate', '--skip-download',
                '--write-subs', '--write-auto-subs', '--sub-langs', 'ko-orig,ko',
                '--sub-format', 'vtt/best', '--retries', '0', '--extractor-retries', '0',
                '--socket-timeout', '20', '-o', str(Path(directory) / '%(id)s.%(ext)s'),
                'https://www.youtube.com/watch?v=' + video_id,
            ]
            completed = subprocess.run(command, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       timeout=90, check=False)
            if completed.returncode:
                return {**base, 'status': 'error', 'error': 'youtube_subtitle_request_failed'}
            # ko-orig is the original-language automatic caption track; ko may
            # be a creator-provided Korean track or a translated track.
            paths = [(language, Path(directory) / f'{video_id}.{language}.vtt')
                     for language in ('ko-orig', 'ko')]
            for language, path in paths:
                if path.is_file():
                    if path.stat().st_size > CAPTION_LIMIT:
                        return {**base, 'status': 'error', 'error': 'youtube_subtitle_too_large'}
                    segments = parse_vtt(path.read_text(encoding='utf-8-sig'))
                    return {**base, 'status': 'available', 'language': language,
                            'segments': segments, 'text': '\n'.join(s['text'] for s in segments)}
            if list(Path(directory).iterdir()):
                return {**base, 'status': 'error', 'error': 'youtube_subtitle_format_unsupported'}
            return base
    except subprocess.TimeoutExpired:
        return {**base, 'status': 'error', 'error': 'youtube_subtitle_timeout'}
    except (OSError, UnicodeError, CollectionError):
        return {**base, 'status': 'error', 'error': 'youtube_subtitle_invalid'}


def collect(source: dict, previous: dict) -> list[dict]:
    """Collect the newest configured window; absent older videos aren't deleted.

    Subtitle failures and missing captions remain retryable on a later run.
    Available captions are reused only for unchanged metadata unless explicitly
    refreshed. This function does not infer stock names or participation.
    """
    maximum = source.get('max_videos', 3)
    refresh = source.get('refresh_subtitles', False)
    hours = source.get('subtitle_refresh_hours', DEFAULT_SUBTITLE_REFRESH_HOURS)
    if (type(maximum) is not int or not 1 <= maximum <= 50 or type(refresh) is not bool
            or type(hours) not in (int, float) or not 0 <= hours <= MAX_SUBTITLE_REFRESH_HOURS):
        raise CollectionError('youtube_options_invalid')
    raw = _feed(source)
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        raise CollectionError('youtube_feed_parse_error') from None
    channel_id = source['channel_id']
    # The live Atom feed may omit the UC prefix in yt:channelId while
    # channel links still carry the full configured channel ID.
    channel_ids = {channel_id, channel_id[2:]}
    if root.tag != ATOM + 'feed' or root.findtext(YT + 'channelId') not in channel_ids:
        raise CollectionError('youtube_feed_channel_mismatch')
    metadata = []
    for entry in root.findall(ATOM + 'entry'):
        video_id = entry.findtext(YT + 'videoId', '')
        title = entry.findtext(ATOM + 'title', '').strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id) or not title:
            raise CollectionError('youtube_feed_entry_invalid')
        if entry.findtext(YT + 'channelId', channel_id) not in channel_ids:
            raise CollectionError('youtube_feed_channel_mismatch')
        published = entry.findtext(ATOM + 'published', '')
        summary = entry.findtext(MEDIA + 'group/' + MEDIA + 'description', '')
        updated = entry.findtext(ATOM + 'updated', '')
        metadata.append((video_id, title, published, summary, updated))
    result, seen = [], set()
    for video_id, title, published, summary, updated in metadata:
        if video_id in seen:
            continue
        seen.add(video_id)
        if len(result) >= maximum:
            break
        metadata_hash = sha256('\0'.join((title, summary, published, updated)).encode()).hexdigest()
        prior = previous.get(video_id, {})
        cached = prior.get('captions', {}) if isinstance(prior, dict) else {}
        if not refresh and not _subtitle_due(cached, metadata_hash, hours):
            captions = cached
        else:
            captions = {**_captions(video_id, metadata_hash), 'last_checked_at': _now().isoformat()}
        result.append({
            'item_id': video_id, 'title': title,
            'url': 'https://www.youtube.com/watch?v=' + video_id,
            'published_raw': published, 'published_at': published_time(published),
            'payload': {'summary': summary, 'channel_id': channel_id,
                        'metadata_updated': updated, 'captions': captions},
        })
    return result
