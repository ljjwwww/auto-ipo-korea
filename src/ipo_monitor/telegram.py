"""Explicit Telegram delivery with durable, conservative per-message receipts.

A timeout cannot prove whether Telegram delivered a message. Such receipts remain
unknown and are not retried automatically. Existing preview rows stay immutable.
"""
from contextlib import closing
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
import json
from pathlib import Path
import re
import sqlite3
import time

import httpx

from . import CollectionError, terminal_text
from .daily_state import file_lock
from .decision import VERSION as POLICY_ENGINE, load_policy
from .evidence import build_inputs, context_hash
from .extraction import digest

MAX_TEXT_UNITS = 3500
MAX_FAILED_ATTEMPTS = 3
_TOKEN = re.compile(r'[0-9]{1,20}:[A-Za-z0-9_-]{20,200}')
_CHAT = re.compile(r'[1-9][0-9]{0,19}')


class TelegramError(CollectionError):
    def __init__(self, code, *, uncertain=False):
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain


def request_api(token, method, payload):
    """Call only supported fixed-host methods; no token-bearing exception escapes."""
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise TelegramError('telegram_token_invalid')
    if method not in {'sendMessage', 'getMe', 'getUpdates'} or not isinstance(payload, dict):
        raise TelegramError('telegram_request_invalid')
    sending = method == 'sendMessage'
    try:
        response = httpx.post(f'https://api.telegram.org/bot{token}/{method}',
                              json=payload, timeout=20, follow_redirects=False,
                              trust_env=False)
    except (httpx.RequestError, UnicodeError):
        raise TelegramError('telegram_network_error', uncertain=sending) from None
    if response.is_redirect:
        raise TelegramError('telegram_redirect_blocked', uncertain=sending)
    if response.status_code >= 500:
        raise TelegramError('telegram_server_error', uncertain=sending)
    if response.status_code >= 400:
        # No raw descriptions/URLs are used: they may contain the bot token.
        raise TelegramError(f'telegram_http_{response.status_code}')
    try:
        if len(response.content) > 2 * 1024 * 1024:
            raise ValueError
        value = response.json()
        if not isinstance(value, dict) or type(value.get('ok')) is not bool:
            raise ValueError
        if value['ok'] is False:
            code = value.get('error_code')
            if type(code) is int and 400 <= code < 500:
                raise TelegramError(f'telegram_api_{code}')
            raise TelegramError('telegram_api_error', uncertain=sending)
        result = value['result']
        if method == 'sendMessage':
            if (not isinstance(result, dict) or type(result.get('message_id')) is not int
                    or result['message_id'] < 0):
                raise ValueError
        elif method == 'getUpdates':
            if not isinstance(result, list):
                raise ValueError
        elif not isinstance(result, dict) or type(result.get('id')) is not int:
            raise ValueError
        return result
    except (ValueError, KeyError, TypeError):
        raise TelegramError('telegram_response_invalid', uncertain=sending) from None


def _clean(value, limit=160):
    return ' '.join(terminal_text(str(value)).split())[:limit]


def _units(text):
    return len(text.encode('utf-16-le')) // 2


def split_messages(text, limit=MAX_TEXT_UNITS):
    """Bound by UTF-16 code units too, including astral symbols/emoji."""
    if type(limit) is not int or not 100 <= limit <= MAX_TEXT_UNITS:
        raise CollectionError('telegram_message_limit_invalid')
    chunks = []; current = ''; units = 0
    for char in text:
        size = _units(char)
        if units + size > limit:
            chunks.append(current)
            current = ''; units = 0
        current += char; units += size
    if current:
        chunks.append(current)
    return chunks


def _stock_message(stock, result):
    lines = [f"\n{_clean(stock)} — {_clean(result.get('status', '판단 대기'))}"]
    selected = [s for s in result.get('contributions', []) if s.get('selected')]
    present = set()
    for signal in selected:
        kind = signal.get('source_type'); present.add(kind)
        label = {'rss': '블로그', 'youtube': '유튜브', 'ipo_site': '사이트'}.get(kind, _clean(kind))
        opinion = _clean(signal.get('opinion', '판단 불가'))
        status = signal.get('analysis_status')
        state = {'unprocessed': '분석 대기', 'error': '분석 실패'}.get(status)
        description = state or opinion
        marker = signal.get('marker') or ''
        rating = signal.get('rating')
        # Raw rating remains visible even if semantic analysis is pending/failed.
        if kind == 'ipo_site':
            if marker:
                description += f" / 표시 {_clean(marker, 30)}"
            elif rating is not None:
                description += f" / 별 {rating}개"
            else:
                description += ' / 별점 미표시'
        lines.append(f"- {_clean(signal.get('creator', ''))} {label}: {description}")
        if kind == 'ipo_site':
            raw = signal.get('raw_ratings', [])
            dates = sorted({r['date'] for r in raw if isinstance(r, dict)
                            and r.get('kind') == 'subscription' and isinstance(r.get('date'), str)})
            if dates:
                lines.append('  청약 일정: ' + ', '.join(_clean(d, 30) for d in dates[:4]))
            if signal.get('rating_conflict'):
                markers = sorted({_clean(r.get('raw_marker'), 30) for r in raw
                                  if isinstance(r, dict) and r.get('raw_marker')})
                lines.append('  상충 원문 표시: ' + ' / '.join(markers[:6]))
        if signal.get('source_status') != 'success':
            lines.append('  수집 상태: 확인 필요')
        if status == 'success' and signal.get('evidence'):
            lines.append('  근거: ' + _clean(signal['evidence'], 100))
        url = signal.get('url')
        if isinstance(url, str) and url.startswith(('https://', 'http://')):
            lines.append('  ' + _clean(url, 400))
    for kind, label in [('rss', '블로그'), ('youtube', '유튜브'), ('ipo_site', '사이트')]:
        if kind not in present:
            lines.append(f'- {label}: 연결 자료 없음 (실제 자료 부재를 뜻하지 않음)')
    return '\n'.join(lines)


def _today():
    return datetime.now(ZoneInfo('Asia/Seoul')).date()


def _subscription_ended(entries, today):
    """Use structured subscription dates, never listing dates or AI guesses.

    A range ends on its latest day. Missing/malformed schedule data is retained
    rather than treated as expired. Filtering affects delivery only, not storage.
    """
    dates=[]
    for entry in entries:
        source=entry['input']
        if source['source_type']!='ipo_site':
            continue
        for event in source['content'].get('events',[]):
            if event.get('kind')!='subscription':
                continue
            value=event.get('date')
            if not isinstance(value,str):
                return False
            try:
                parsed=date.fromisoformat(value)
            except ValueError:
                return False
            if parsed.isoformat()!=value:
                return False
            dates.append(parsed)
    return bool(dates) and max(dates)<today


def render_digest(db, aliases, policy):
    """Read current verified-context Ollama decisions only; no network/writes."""
    summary = dict(stocks=0, transitions=[], chunks=[], identity=None,
                   skipped_fixture=0, skipped_stale=0, skipped_expired=0)
    db = Path(db).resolve()
    if not db.is_file():
        return summary
    try:
        with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'decision_versions', 'decision_transitions', 'decision_transition_current'}.issubset(tables):
                return summary
            entries = build_inputs(conn, Path(aliases))
            contexts = {stock: context_hash([e for e in entries if e['input']['stock'] == stock])
                        for stock in {e['input']['stock'] for e in entries}}
            policy_hash = digest({'engine': POLICY_ENGINE, 'policy': load_policy(Path(policy))})
            rows = conn.execute('''SELECT v.*,t.id AS transition_id FROM decision_versions v
                JOIN decision_transitions t ON t.version_id=v.id
                JOIN decision_transition_current c ON c.transition_id=t.id
                ORDER BY v.stock''').fetchall()
        messages = []
        today = _today()
        for row in rows:
            if contexts.get(row['stock']) != row['context_hash'] or row['policy_hash'] != policy_hash:
                summary['skipped_stale'] += 1
                continue
            result = json.loads(row['result_json'])
            if result.get('analysis_origin') != 'ollama':
                summary['skipped_fixture'] += 1
                continue
            stock_entries=[e for e in entries if e['input']['stock']==row['stock']]
            if _subscription_ended(stock_entries,today):
                summary['skipped_expired']+=1
                continue
            messages.append(_stock_message(row['stock'], result))
            summary['transitions'].append(row['transition_id'])
        summary['stocks'] = len(messages)
        if messages:
            text = '[공모주 모니터링 · 실험 판단]\n최신 수집 자료 기준이며 참여 확정·주문 지시가 아닙니다.\n'
            text += '\n'.join(messages)
            text += '\n\n판단 불가는 미참여가 아닙니다. 분석 대기·수집 실패는 별도 확인이 필요합니다.'
            summary['chunks'] = split_messages(text)
            summary['identity'] = digest({'version': 'telegram-digest-v1',
                                          'transitions': summary['transitions'], 'chunks': summary['chunks']})
        return summary
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        raise CollectionError('telegram_digest_invalid') from None


def _schema(conn):
    conn.executescript('''CREATE TABLE IF NOT EXISTS telegram_batches (
        id INTEGER PRIMARY KEY, destination_hash TEXT NOT NULL, digest_hash TEXT NOT NULL,
        created_at TEXT NOT NULL, UNIQUE(destination_hash,digest_hash));
        CREATE TABLE IF NOT EXISTS telegram_chunks (
        batch_id INTEGER NOT NULL REFERENCES telegram_batches(id), position INTEGER NOT NULL,
        text TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','sending','sent','failed','unknown')),
        attempts INTEGER NOT NULL DEFAULT 0, message_id INTEGER, error_code TEXT,
        updated_at TEXT NOT NULL, PRIMARY KEY(batch_id,position));''')
    conn.commit()


def _now():
    return datetime.now(timezone.utc).isoformat()


def send_daily(db, aliases, policy, state_db, credential=None, dry_run=False, allow_send=False):
    """Send one deduplicated current-state digest; no automatic uncertain retries."""
    rendered = render_digest(db, aliases, policy)
    summary = dict(status='preview' if dry_run else 'empty', sent=0, unknown=0, failed=0,
                   stocks=rendered['stocks'], chunks=len(rendered['chunks']),
                   skipped_stale=rendered['skipped_stale'], skipped_fixture=rendered['skipped_fixture'],
                   skipped_expired=rendered.get('skipped_expired',0))
    if dry_run:
        summary['messages'] = rendered['chunks']
        return summary
    if not allow_send:
        raise CollectionError('telegram_send_requires_opt_in')
    if not rendered['chunks']:
        return summary
    if (not isinstance(credential, dict) or not isinstance(credential.get('token'), str)
            or not _TOKEN.fullmatch(credential['token'])
            or not _CHAT.fullmatch(str(credential.get('chat_id', '')))):
        raise CollectionError('telegram_credentials_invalid')
    state_db = Path(state_db)
    if state_db.is_symlink():
        raise CollectionError('telegram_state_path_invalid')
    state_db.parent.mkdir(parents=True, exist_ok=True)
    destination = digest({'bot': credential['token'].split(':', 1)[0], 'chat': str(credential['chat_id'])})
    with file_lock(state_db.with_suffix('.lock')), closing(sqlite3.connect(state_db)) as conn:
        conn.row_factory = sqlite3.Row
        state_db.chmod(0o600); _schema(conn)
        with conn:
            conn.execute('INSERT OR IGNORE INTO telegram_batches(destination_hash,digest_hash,created_at) VALUES (?,?,?)',
                         (destination, rendered['identity'], _now()))
            batch_id = conn.execute('SELECT id FROM telegram_batches WHERE destination_hash=? AND digest_hash=?',
                                    (destination, rendered['identity'])).fetchone()[0]
            conn.executemany('''INSERT OR IGNORE INTO telegram_chunks
                (batch_id,position,text,state,updated_at) VALUES (?,?,?,'pending',?)''',
                [(batch_id, i, text, _now()) for i, text in enumerate(rendered['chunks'])])
            # A crash during the last transport leaves delivery ambiguous, not retryable.
            conn.execute("UPDATE telegram_chunks SET state='unknown',error_code='telegram_interrupted',updated_at=? WHERE batch_id=? AND state='sending'",
                         (_now(), batch_id))
        rows = conn.execute('SELECT * FROM telegram_chunks WHERE batch_id=? ORDER BY position', (batch_id,)).fetchall()
        blocked_unknown = any(row['state'] == 'unknown' for row in rows)
        dispatched = 0
        for row in ([] if blocked_unknown else rows):
            if row['state'] == 'failed' and row['attempts'] >= MAX_FAILED_ATTEMPTS:
                # An exhausted head must not let later digest parts bypass it.
                break
            if row['state'] in {'sent', 'unknown'}:
                continue
            if dispatched:
                time.sleep(1.05)
            dispatched += 1
            with conn:
                conn.execute("UPDATE telegram_chunks SET state='sending',attempts=attempts+1,error_code=NULL,updated_at=? WHERE batch_id=? AND position=?",
                             (_now(), batch_id, row['position']))
            try:
                response = request_api(credential['token'], 'sendMessage',
                                       {'chat_id': str(credential['chat_id']), 'text': row['text'],
                                        'link_preview_options': {'is_disabled': True}})
            except TelegramError as exc:
                state = 'unknown' if exc.uncertain else 'failed'
                with conn:
                    conn.execute('UPDATE telegram_chunks SET state=?,error_code=?,updated_at=? WHERE batch_id=? AND position=?',
                                 (state, exc.code, _now(), batch_id, row['position']))
                # Do not flood the destination or reorder a partially sent digest.
                break
            else:
                with conn:
                    conn.execute("UPDATE telegram_chunks SET state='sent',message_id=?,updated_at=? WHERE batch_id=? AND position=?",
                                 (response['message_id'], _now(), batch_id, row['position']))
                summary['sent'] += 1
        states = dict(conn.execute('SELECT state,count(*) FROM telegram_chunks WHERE batch_id=? GROUP BY state', (batch_id,)).fetchall())
        summary.update(unknown=states.get('unknown', 0), failed=states.get('failed', 0),
                       pending=states.get('pending', 0))
        summary['status'] = ('unknown' if summary['unknown'] else 'failed' if summary['failed'] else
                             'pending' if summary['pending'] else 'sent' if summary['sent'] else 'unchanged')
    return summary
