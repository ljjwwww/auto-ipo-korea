"""Telegram receipts for individual new mention events, never stock digests."""
from contextlib import closing
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from . import CollectionError, terminal_text
from .event_validation import validate_event_response
from .daily_state import file_lock
from .extraction import digest
from .telegram import TelegramError, request_api

MAX_ATTEMPTS = 3
_TOKEN = re.compile(r'[1-9][0-9]{0,19}:[A-Za-z0-9_-]{20,200}')
_CHAT = re.compile(r'[1-9][0-9]{0,18}')


def _today():
    return datetime.now(ZoneInfo('Asia/Seoul')).date()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _date(value):
    if not isinstance(value, str):
        raise ValueError
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError
    return result


def _clean(value, limit):
    return ' '.join(terminal_text(str(value)).split())[:limit]


def _destination(credential):
    if (not isinstance(credential, dict) or not isinstance(credential.get('token'), str)
            or not _TOKEN.fullmatch(credential['token'])
            or not isinstance(credential.get('chat_id'), str)
            or not _CHAT.fullmatch(credential['chat_id'])):
        raise CollectionError('mention_telegram_credentials_invalid')
    return digest({'bot': credential['token'].split(':', 1)[0], 'chat': credential['chat_id']})


def _url(value, kind):
    """Only configured source types/hosts become a clickable event source."""
    if (not isinstance(value, str) or len(value) > 1200
            or any(ord(char) < 33 or char == '\\' for char in value)):
        raise ValueError
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise ValueError
    if kind == 'rss':
        if (parsed.hostname not in {'blog.naver.com', 'm.blog.naver.com'}
                or not re.fullmatch(r'/hycj1025/[0-9]+', parsed.path)):
            raise ValueError
    elif kind == 'youtube':
        if parsed.hostname in {'www.youtube.com', 'youtube.com'}:
            video = parse_qs(parsed.query).get('v', [])
            if parsed.path != '/watch' or len(video) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]{11}', video[0]):
                raise ValueError
        elif parsed.hostname == 'youtu.be':
            if not re.fullmatch(r'/[A-Za-z0-9_-]{11}', parsed.path):
                raise ValueError
        else:
            raise ValueError
    else:
        raise ValueError
    return value


def _message(row):
    source = json.loads(row['input_json'])
    result = json.loads(row['analysis_json'])
    if (not isinstance(source, dict) or source.get('stock') != row['stock']
            or source.get('source_type') != row['source_type']
            or source.get('source_id') != row['source_id']):
        raise ValueError
    validate_event_response(result, source)
    url = _url(source.get('url'), row['source_type'])
    stock = _clean(row['stock'], 80)
    creator = _clean(source.get('creator', ''), 40)
    if not stock or not creator:
        raise ValueError
    kind = {'rss': '블로그', 'youtube': '유튜브'}[row['source_type']]
    return (f"[{stock}] {creator} {kind}: {_clean(result['reason'], 180)} "
            f"(의견: {result['opinion']})\n{url}")


def _schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS mention_delivery (
        event_id INTEGER NOT NULL, destination_hash TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('sending','sent','failed','unknown')),
        attempts INTEGER NOT NULL DEFAULT 0, message_id INTEGER,
        error_code TEXT, last_attempt_day TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(event_id,destination_hash))''')
    conn.commit()


def _tables(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _closed_events(conn):
    """Optional parent-owned controls table; absent means nothing is closed."""
    if 'mention_event_controls' not in _tables(conn):
        return set()
    columns = {row[1] for row in conn.execute('PRAGMA table_info(mention_event_controls)')}
    if not {'event_id', 'closed'}.issubset(columns):
        return set()
    return {row[0] for row in conn.execute('SELECT event_id FROM mention_event_controls WHERE closed=1')}


def _load(conn, today, summary, destination=None):
    tables = _tables(conn)
    if not {'mention_settings', 'mention_catalog', 'mention_events'}.issubset(tables):
        return []
    closed = _closed_events(conn)
    setting = conn.execute("SELECT value FROM mention_settings WHERE key='catalog_date'").fetchone()
    if not setting or setting[0] != today.isoformat():
        summary['status'] = 'stale_catalog'
        return []
    month = today.strftime('%Y-%m')
    catalog = {}
    for stock, payload_json in conn.execute('SELECT stock,payload_json FROM mention_catalog WHERE month=?', (month,)):
        try:
            payload = json.loads(payload_json)
            start, end = _date(payload['start']), _date(payload['end'])
            if start > end:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            summary['invalid'] += 1
            continue
        if start.strftime('%Y-%m') <= month <= end.strftime('%Y-%m'):
            catalog[stock] = end
    candidates = []
    for row in conn.execute('SELECT * FROM mention_events ORDER BY id'):
        if row['id'] in closed:
            summary['closed'] += 1
            continue
        if row['source_type'] not in {'rss', 'youtube'}:
            summary['skipped_source'] += 1
            continue
        if row['stock'] not in catalog:
            summary['not_in_catalog'] += 1
            continue
        if catalog[row['stock']] < today:
            summary['expired'] += 1
            continue
        if row['analysis_status'] != 'success':
            summary['pending'] += 1
            continue
        if 'analysis_origin' not in row.keys() or row['analysis_origin'] != 'ollama':
            summary['skipped_fixture'] += 1
            continue
        receipt = _receipt(conn, row['id'], destination, tables)
        if receipt is not None and receipt['state'] == 'sent':
            summary['unchanged'] += 1
            continue
        try:
            message = _message(row)
        except (CollectionError, ValueError, KeyError, TypeError):
            summary['invalid'] += 1
            continue
        candidates.append((row['id'], message))
    return candidates


def _receipt(conn, event_id, destination, tables):
    if 'mention_delivery' not in tables:
        return None
    if destination is None:
        # Credential-free previews conservatively suppress already confirmed or
        # ambiguous receipts for any destination rather than presenting a resend.
        return conn.execute('''SELECT * FROM mention_delivery WHERE event_id=?
            ORDER BY CASE state WHEN 'sent' THEN 0 WHEN 'unknown' THEN 1
            WHEN 'sending' THEN 2 ELSE 3 END LIMIT 1''', (event_id,)).fetchone()
    return conn.execute('SELECT * FROM mention_delivery WHERE event_id=? AND destination_hash=?',
                        (event_id, destination)).fetchone()


def _classify(summary, candidates, conn, destination, today):
    """Count current eligible receipts without exposing event/chat identifiers."""
    tables = _tables(conn)
    ready = []
    for event_id, message in candidates:
        row = _receipt(conn, event_id, destination, tables)
        if row is None:
            ready.append((event_id, message))
        elif row['state'] == 'sent':
            summary['unchanged'] += 1
        elif row['state'] in {'unknown', 'sending'}:
            summary['unknown'] += 1
        elif row['attempts'] >= MAX_ATTEMPTS or row['last_attempt_day'] >= today.isoformat():
            summary['failed'] += 1
        else:
            ready.append((event_id, message))
    return ready


def deliver_events(event_db, credential=None, allow_send=False, dry_run=False, today=None):
    """Send only analyzed new source events in a freshly checked monthly catalog.

    Known rejection retries are at most once per Korean day, three attempts total.
    Timeouts and crash-interrupted requests remain unknown, never auto-retried.
    """
    today = _today() if today is None else today
    if type(today) is not date:
        raise CollectionError('mention_delivery_date_invalid')
    if not dry_run and allow_send is not True:
        raise CollectionError('mention_delivery_requires_opt_in')
    summary = dict(status='preview' if dry_run else 'empty', sent=0, failed=0,
                   unknown=0, pending=0, expired=0, not_in_catalog=0, closed=0,
                   skipped_source=0, skipped_fixture=0, invalid=0, unchanged=0)
    path = Path(event_db).resolve()
    if not path.is_file():
        if dry_run:
            summary['messages'] = []
        return summary
    if Path(event_db).is_symlink():
        raise CollectionError('mention_delivery_path_invalid')
    destination = _destination(credential) if credential is not None else None
    try:
        if dry_run:
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                candidates = _load(conn, today, summary, destination)
                ready = _classify(summary, candidates, conn, destination, today)
                summary['messages'] = [message for _, message in ready]
                summary['pending'] += len(ready)
            return summary
        with file_lock(path.with_suffix('.delivery.lock')), closing(sqlite3.connect(path, timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            candidates = _load(conn, today, summary, destination)
            if summary['status'] == 'stale_catalog':
                return summary
            if not candidates:
                if summary['invalid']:
                    summary['status'] = 'error'
                elif summary['pending']:
                    summary['status'] = 'pending'
                elif summary['unchanged']:
                    summary['status'] = 'unchanged'
                return summary
            if destination is None:
                destination = _destination(credential)
            path.chmod(0o600)
            _schema(conn)
            with conn:
                conn.execute("UPDATE mention_delivery SET state='unknown',error_code='telegram_interrupted',updated_at=? WHERE destination_hash=? AND state='sending'",
                             (_now(), destination))
            ready = _classify(summary, candidates, conn, destination, today)
            dispatched = 0
            for position, (event_id, message) in enumerate(ready):
                if dispatched:
                    time.sleep(1.05)
                with conn:
                    conn.execute('''INSERT INTO mention_delivery
                        (event_id,destination_hash,state,attempts,last_attempt_day,updated_at)
                        VALUES (?,?,'sending',1,?,?) ON CONFLICT(event_id,destination_hash)
                        DO UPDATE SET state='sending',attempts=attempts+1,message_id=NULL,
                        error_code=NULL,last_attempt_day=excluded.last_attempt_day,updated_at=excluded.updated_at''',
                        (event_id, destination, today.isoformat(), _now()))
                dispatched += 1
                try:
                    result = request_api(credential['token'], 'sendMessage',
                                         {'chat_id': credential['chat_id'], 'text': message,
                                          'link_preview_options': {'is_disabled': True}})
                except TelegramError as exc:
                    state = 'unknown' if exc.uncertain else 'failed'
                    with conn:
                        conn.execute('''UPDATE mention_delivery SET state=?,error_code=?,updated_at=?
                            WHERE event_id=? AND destination_hash=?''',
                            (state, exc.code, _now(), event_id, destination))
                    summary[state] += 1
                    summary['pending'] += len(ready)-position-1
                    break
                else:
                    with conn:
                        conn.execute('''UPDATE mention_delivery SET state='sent',message_id=?,updated_at=?
                            WHERE event_id=? AND destination_hash=?''',
                            (result['message_id'], _now(), event_id, destination))
                    summary['sent'] += 1
            summary['status'] = ('unknown' if summary['unknown'] else 'failed' if summary['failed'] else
                                 'error' if summary['invalid'] else 'sent' if summary['sent'] else
                                 'pending' if summary['pending'] else 'unchanged' if summary['unchanged'] else 'empty')
        return summary
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        raise CollectionError('mention_delivery_storage_invalid') from None
