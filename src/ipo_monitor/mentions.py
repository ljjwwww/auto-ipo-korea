"""Monthly IPO catalogue and durable new-mention discovery, without AI or sends."""
from datetime import date, datetime
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from .evidence import build_inputs
from .extraction import digest


VERSION = 'monthly-mentions-v1'


def ensure_schema(conn):
    """Create additive local event tables without committing a caller transaction."""
    statements = [
        '''CREATE TABLE IF NOT EXISTS mention_events (
            id INTEGER PRIMARY KEY, event_hash TEXT NOT NULL UNIQUE,
            stock TEXT NOT NULL, source_id TEXT NOT NULL, source_type TEXT NOT NULL,
            item_id TEXT NOT NULL, snapshot_id INTEGER NOT NULL,
            input_hash TEXT NOT NULL, input_json TEXT NOT NULL, created_at TEXT NOT NULL,
            analysis_json TEXT, analysis_origin TEXT NOT NULL DEFAULT 'unknown',
            analysis_status TEXT NOT NULL DEFAULT 'pending',
            error_code TEXT, attempts INTEGER NOT NULL DEFAULT 0)''',
        '''CREATE TABLE IF NOT EXISTS mention_seen (
            source_id TEXT NOT NULL, item_id TEXT NOT NULL, content_hash TEXT NOT NULL,
            PRIMARY KEY(source_id,item_id))''',
        '''CREATE TABLE IF NOT EXISTS mention_source_baselines (
            source_id TEXT PRIMARY KEY)''',
        '''CREATE TABLE IF NOT EXISTS mention_settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS mention_catalog (
            month TEXT NOT NULL, stock TEXT NOT NULL, payload_json TEXT NOT NULL,
            PRIMARY KEY(month,stock))''',
        'CREATE INDEX IF NOT EXISTS mention_events_pending ON mention_events(analysis_status,id)',
    ]
    for statement in statements:
        conn.execute(statement)
    columns={row[1] for row in conn.execute("PRAGMA table_info(mention_events)")}
    if "analysis_origin" not in columns:
        conn.execute("ALTER TABLE mention_events ADD COLUMN analysis_origin TEXT NOT NULL DEFAULT 'unknown'")


def _iso_date(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _subscription_dates(content):
    events = [event for event in content.get('events', []) if event.get('kind') == 'subscription']
    if events:
        dates = [_iso_date(event.get('date')) for event in events]
        # A partial/malformed range is not safe to use as an eligibility rule.
        return dates if all(value is not None for value in dates) else []
    dates = []
    for row in content.get('table_rows', []):
        raw = row.get('fields', {}).get('청약일', '')
        if not isinstance(raw, str):
            continue
        # MM.DD does not identify a year. Only explicit ISO table dates qualify.
        values = re.findall(r'(?<![0-9])(\d{4}-\d{2}-\d{2})(?![0-9])', raw)
        if not values:
            continue
        parsed = [_iso_date(value) for value in values]
        if not all(value is not None for value in parsed):
            return []
        dates.extend(parsed)
    return dates


def monthly_catalog(source_conn, aliases: Path, today: date):
    """Return active subscription windows intersecting today's Korean month.

    Known calendar dates take precedence; neither listing dates nor yearless
    table text are guessed into subscription windows. Observations stay intact.
    """
    month_start = today.replace(day=1)
    next_month = (date(today.year + 1, 1, 1) if today.month == 12
                  else date(today.year, today.month + 1, 1))
    candidates = {}
    for entry in build_inputs(source_conn, Path(aliases)):
        source = entry['input']
        if source['source_type'] != 'ipo_site':
            continue
        content = source['content']
        dates = _subscription_dates(content)
        if not dates:
            continue
        start, end = min(dates), max(dates)
        if end < today or end < month_start or start >= next_month:
            continue
        stock = source['stock']
        fields = {}
        for row in content.get('table_rows', []):
            for key, value in row.get('fields', {}).items():
                if isinstance(key, str) and isinstance(value, str):
                    fields[key] = value
        record = {'stock': stock, 'start': start.isoformat(), 'end': end.isoformat(),
                  'events': content.get('events', []), 'fields': fields,
                  'source_id': source['source_id'], 'url': source['url']}
        # Keep the latest current observation if more than one site item maps to
        # the same explicitly registered stock/alias.
        order = (entry['observed_at'], entry['snapshot_id'])
        if stock not in candidates or order > candidates[stock][0]:
            candidates[stock] = (order, record)
    return {stock: record for stock, (_, record) in sorted(candidates.items())}


def _site_current(source_conn):
    sources = source_conn.execute("SELECT id FROM tracked_sources WHERE type='ipo_site'").fetchall()
    if not sources:
        return False
    for source in sources:
        latest = source_conn.execute(
            'SELECT status FROM source_attempts WHERE source_id=? ORDER BY id DESC LIMIT 1',
            (source['id'],)).fetchone()
        if latest is None or latest['status'] != 'success':
            return False
    return True


def discover(source_conn, aliases: Path, state_conn, today: date):
    """Refresh the month's catalogue and queue new literal blog/video mentions.

    The first successful discovery establishes a baseline, never an archive
    blast. A failed site refresh does not advance the baseline or seen hashes,
    so new source revisions remain discoverable after recovery.
    """
    ensure_schema(state_conn)
    month = today.strftime('%Y-%m')
    summary = {'month': month, 'catalog': 0, 'baseline': False, 'seen': 0,
               'changed': 0, 'queued': 0, 'ambiguous': 0, 'stale_catalog': False}
    if not _site_current(source_conn):
        summary['stale_catalog'] = True
        with state_conn:
            state_conn.execute("DELETE FROM mention_settings WHERE key='catalog_date'")
        return summary
    catalog = monthly_catalog(source_conn, aliases, today)
    rows = source_conn.execute('''SELECT a.*,s.type FROM source_snapshots a
        JOIN tracked_sources s ON s.id=a.source_id
        WHERE s.type IN ('rss','youtube') AND a.id=(
            SELECT MAX(b.id) FROM source_snapshots b
            WHERE b.source_id=a.source_id AND b.item_id=a.item_id)
        ORDER BY a.source_id,a.item_id''').fetchall()
    initialized = state_conn.execute(
        "SELECT value FROM mention_settings WHERE key='initialized'").fetchone() is not None
    baselines = {row[0] for row in state_conn.execute('SELECT source_id FROM mention_source_baselines')}
    available = set()
    row_sources = {row['source_id'] for row in rows}
    for source in source_conn.execute("SELECT id FROM tracked_sources WHERE type IN ('rss','youtube')"):
        latest = source_conn.execute(
            'SELECT status FROM source_attempts WHERE source_id=? ORDER BY id DESC LIMIT 1',
            (source['id'],)).fetchone()
        # Production collectors record success/partial even for a valid empty
        # result. Existing synthetic/imported observations may have no attempt.
        if ((latest is not None and latest['status'] in {'success', 'partial'})
                or (latest is None and source['id'] in row_sources)):
            available.add(source['id'])
    rows = [row for row in rows if row['source_id'] in available]
    changed = {}
    for row in rows:
        # Each source must first establish its own baseline. A source whose
        # initial fetch failed must not alert on its recovered archive later.
        if row['source_id'] not in baselines:
            continue
        prior = state_conn.execute(
            'SELECT content_hash FROM mention_seen WHERE source_id=? AND item_id=?',
            (row['source_id'], row['item_id'])).fetchone()
        if prior is None or prior[0] != row['content_hash']:
            changed[row['id']] = row
    entries = (build_inputs(source_conn, Path(aliases), list(catalog))
               if initialized and changed and catalog else [])
    timestamp = datetime.now(ZoneInfo('Asia/Seoul')).isoformat()
    with state_conn:
        state_conn.executemany('INSERT OR IGNORE INTO mention_source_baselines(source_id) VALUES (?)',
                               [(source_id,) for source_id in sorted(available)])
        state_conn.execute('DELETE FROM mention_catalog WHERE month=?', (month,))
        state_conn.executemany('INSERT INTO mention_catalog(month,stock,payload_json) VALUES (?,?,?)',
            [(month, stock, json.dumps(record, ensure_ascii=False, sort_keys=True))
             for stock, record in catalog.items()])
        for key, value in [('catalog_date', today.isoformat()), ('initialized', '1')]:
            state_conn.execute('''INSERT INTO mention_settings(key,value) VALUES (?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value''', (key, value))
        if initialized:
            for entry in entries:
                source = entry['input']
                row = changed.get(entry['snapshot_id'])
                if (row is None or source['source_type'] not in {'rss', 'youtube'}
                        or source['stock'] not in catalog):
                    continue
                if entry.get('match_quality') != 'literal_entity':
                    summary['ambiguous'] += 1
                    continue
                event_hash = digest({'version': VERSION, 'source_id': source['source_id'],
                                     'item_id': row['item_id'], 'content_hash': row['content_hash'],
                                     'stock': source['stock'], 'input_hash': entry['input_hash']})
                inserted = state_conn.execute('''INSERT OR IGNORE INTO mention_events
                    (event_hash,stock,source_id,source_type,item_id,snapshot_id,input_hash,input_json,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?)''',
                    (event_hash, source['stock'], source['source_id'], source['source_type'],
                     row['item_id'], entry['snapshot_id'], entry['input_hash'],
                     json.dumps(source, ensure_ascii=False, sort_keys=True), timestamp))
                summary['queued'] += inserted.rowcount
        state_conn.executemany('''INSERT INTO mention_seen(source_id,item_id,content_hash) VALUES (?,?,?)
            ON CONFLICT(source_id,item_id) DO UPDATE SET content_hash=excluded.content_hash''',
            [(row['source_id'], row['item_id'], row['content_hash']) for row in rows])
    summary.update(catalog=len(catalog), baseline=not initialized,
                   seen=len(rows), changed=len(changed) if initialized else 0)
    return summary
