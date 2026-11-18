"""Bounded daily collection/analysis; prepares previews and sends Telegram only after persistent opt-in."""
from contextlib import chdir, closing
from datetime import datetime, timedelta, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sqlite3
import sys
import tomllib
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from . import CollectionError, terminal_text
from .assessment import FixtureAnalyzer, ensure_schema, run_assessment
from .daily_state import DailyLedger, file_lock
from .evidence import build_inputs
from .multisource import collect_sources, connect


def now():
    return datetime.now(ZoneInfo('Asia/Seoul'))


def load_config(path):
    try:
        with Path(path).open('rb') as handle:
            config = tomllib.load(handle)['daily']
        if not isinstance(config, dict):
            raise ValueError
        root = (Path(path).resolve().parent / config.get('project_root', '..')).resolve()
        config['root'] = root
        for field in ('enabled', 'offline'):
            if type(config.get(field)) is not bool:
                raise ValueError
        if config.get('backend') not in {'fixture', 'ollama'}:
            raise ValueError
        for field, maximum in [('max_calls_per_day', 1200), ('max_attempts', 10), ('retry_hours', 168)]:
            if type(config.get(field)) is not int or not 1 <= config[field] <= maximum:
                raise ValueError
        if not isinstance(config.get('model'), str) or not config['model'].strip():
            raise ValueError
        for field in ('sources', 'db', 'ledger', 'aliases', 'policy', 'log'):
            value = config[field]
            if not isinstance(value, str) or not value:
                raise ValueError
            config[field] = (root / value).resolve()
        if config.get('fixture'):
            config['fixture'] = (root / config['fixture']).resolve()
        elif config['backend'] == 'fixture':
            raise ValueError
        if 'telegram_config' in config:
            config['telegram_config'] = (root / config['telegram_config']).resolve()
        runtime=config.get('max_runtime_minutes',20)
        if type(runtime) is not int or not 1 <= runtime <= 25:
            raise ValueError
        config['max_runtime_minutes']=runtime
        config.setdefault('workflow','assessment')
        if config['workflow'] not in {'assessment','mention_events'}:
            raise ValueError
        if config['workflow']=='mention_events':
            config['event_db']=(root/config['event_db']).resolve()
            if config['event_db'] in {config['db'],config['ledger']}:
                raise ValueError
        if config['db'] == config['ledger']:
            raise ValueError
        return config
    except (OSError, KeyError, TypeError, ValueError):
        raise CollectionError('일일 실행 설정 오류: config/daily.toml을 확인하세요.') from None


def _cache_key(config, fixture, h):
    if config['backend'] == 'fixture':
        return fixture.key(h)
    from .ollama import cache_key
    return cache_key(config['model'], h)


def plan_entries(conn, entries, config, ledger_conn, current):
    """New inputs before retries; budget-deferred inputs are never cached as failures."""
    fixture = FixtureAnalyzer(config['fixture']) if config['backend'] == 'fixture' else None
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    eligible = []; counts = dict(cached=0, pending=0, cooldown=0, exhausted=0, ambiguous=0)
    for entry in entries:
        h = entry['input_hash']
        if entry.get('match_quality') == 'substring_candidate':
            counts['ambiguous'] += 1
            continue
        key = _cache_key(config, fixture, h)
        prior = conn.execute('SELECT status FROM observation_analyses WHERE input_hash=? AND backend_key=?', (h, key)).fetchone() if 'observation_analyses' in tables else None
        if prior and prior[0] == 'success':
            counts['cached'] += 1
            continue
        attempts = []
        if ledger_conn is not None:
            attempts = ledger_conn.execute('''SELECT r.started_at FROM daily_reservations a
                JOIN daily_runs r ON r.id=a.run_id WHERE a.input_hash=? AND a.model=? ORDER BY a.id DESC''',
                (h, config['model'])).fetchall()
        counts['pending'] += 1
        if len(attempts) >= config['max_attempts']:
            counts['exhausted'] += 1
        elif attempts and current.astimezone(timezone.utc) - datetime.fromisoformat(attempts[0][0]) < timedelta(hours=config['retry_hours']):
            counts['cooldown'] += 1
        else:
            eligible.append((bool(attempts), entry))
    # Stable item ordering lets uncached inputs move forward on subsequent days.
    eligible.sort(key=lambda row: (row[0], row[1]['input_hash']))
    return [entry for _, entry in eligible], counts


class DailyAnalyzer:
    can_defer = True

    def __init__(self, config, selected, ledger, run_id, day):
        self.config = config; self.selected = set(selected); self.ledger = ledger
        self.run_id = run_id; self.day = day; self.origin = config['backend']
        self.delegate = None; self.calls = 0; self.usage = []
        self.fixture = FixtureAnalyzer(config['fixture']) if self.origin == 'fixture' else None

    def key(self, h):
        return _cache_key(self.config, self.fixture, h)

    def analyze(self, h, source):
        from .assessment import AnalysisDeferred
        if time.monotonic() >= self.config.get('_deadline',float('inf')):
            raise AnalysisDeferred('daily_runtime_pending')
        if now().date().isoformat() != self.day:
            raise AnalysisDeferred('daily_day_changed')
        if h not in self.selected:
            raise AnalysisDeferred('daily_analysis_pending')
        self.selected.remove(h)
        # Load credentials only when there really is an authorized request.
        if self.delegate is None:
            if self.origin == 'fixture':
                self.delegate = self.fixture
            else:
                from .credentials import read_key
                from .ollama import OllamaAnalyzer
                with chdir(self.config['root']):
                    key = read_key()
                self.delegate = OllamaAnalyzer(self.config['model'], True, self.config['max_calls_per_day'], key)
        reservation = self.ledger.reserve(self.day, self.config['model'], self.run_id, h, self.config['max_calls_per_day'])
        if reservation is None:
            raise AnalysisDeferred('daily_budget_pending')
        self.ledger.mark_dispatched(reservation)
        self.calls += 1
        status = 'failed'
        usage_before = len(getattr(self.delegate, 'usage', []))
        try:
            result = self.delegate.analyze(h, source)
            status = 'completed'
            return result
        finally:
            usage = getattr(self.delegate, 'usage', [])
            self.usage = usage
            current_usage = next((item for item in reversed(usage[usage_before:])
                                  if item.get('input_hash') == h), None)
            self.ledger.complete(reservation, current_usage, status=status)


def _log(path, summary):
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.Logger('ipo-daily')
    handler = RotatingFileHandler(path, maxBytes=256_000, backupCount=3, encoding='utf-8')
    logger.addHandler(handler)
    try:
        logger.warning(json.dumps(summary, ensure_ascii=False))
    finally:
        handler.close()


def _dry_run(config):
    summary = dict(status='dry_run', requests=0, messaging='not_sent', enabled=config['enabled'])
    if not config['db'].is_file():
        summary.update(inputs=0, pending=0, remaining=config['max_calls_per_day'])
        return summary
    ledger_conn = None
    try:
        if config['ledger'].is_file():
            ledger_conn = sqlite3.connect(config['ledger'].as_uri() + '?mode=ro', uri=True)
        with closing(sqlite3.connect(config['db'].as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            entries = build_inputs(conn, config['aliases'])
            eligible, counts = plan_entries(conn, entries, config, ledger_conn, now())
        used = ledger_conn.execute('SELECT count(*) FROM daily_reservations WHERE day=?', (now().date().isoformat(),)).fetchone()[0] if ledger_conn else 0
        summary.update(inputs=len(entries), eligible=len(eligible), remaining=max(0, config['max_calls_per_day']-used), **counts)
        return summary
    finally:
        if ledger_conn is not None:
            ledger_conn.close()


def command(args):
    summary = dict(status='error', requests=0, messaging='not_sent')
    try:
        config = load_config(args.config)
        if config['workflow']=='mention_events':
            from .mention_workflow import command as mention_command
            return mention_command(config,args)
        if args.dry_run:
            print(json.dumps(_dry_run(config), ensure_ascii=False)); return 0
        if not config['enabled']:
            print(json.dumps(dict(status='disabled', requests=0, messaging='not_sent'))); return 0
        if config['backend'] == 'ollama' and not args.allow_api:
            raise CollectionError('일일 Ollama 실행에는 --allow-api가 필요합니다. 수집/AI는 실행하지 않았습니다.')
        with file_lock(config['ledger'].with_suffix('.lock')), DailyLedger(config['ledger']) as ledger:
            current = now(); day = current.date().isoformat(); run_id = ledger.start_run(day)
            config['_deadline']=time.monotonic()+60*config['max_runtime_minutes']
            phase = 'collection'
            try:
                collected = collect_sources(SimpleNamespace(config=config['sources'], db=config['db'], offline=config['offline'], source=None))
                phase = 'planning'
                with closing(connect(config['db'])) as conn:
                    ensure_schema(conn)
                    entries = build_inputs(conn, config['aliases'])
                    eligible, counts = plan_entries(conn, entries, config, ledger.conn, current)
                selected = [e['input_hash'] for e in eligible[:ledger.remaining(day, config['max_calls_per_day'])]]
                analyzer = DailyAnalyzer(config, selected, ledger, run_id, day)
                assessed = 0
                phase = 'assessment'
                if entries:
                    assessed = run_assessment(SimpleNamespace(db=config['db'], policy=config['policy'], aliases=config['aliases'],
                        stock=None, backend=config['backend'], fixture=config.get('fixture'), model=config['model'], allow_api=args.allow_api,
                        max_calls=config['max_calls_per_day'], channel='telegram', destination='preview'), analyzer_override=analyzer)
                phase = 'queue_refresh'
                with closing(connect(config['db'])) as conn:
                    _, after = plan_entries(conn, entries, config, ledger.conn, now())
                operational_error = bool(collected or assessed)
                partial = bool(after['pending'] or after['ambiguous'])
                error_code = 'collection_failed' if collected else ('assessment_failed' if assessed else None)
                summary.update(status='error' if operational_error else ('partial' if partial else 'success'),
                               operational_error=operational_error, error_code=error_code,
                               phase='collection' if collected else ('assessment' if assessed else 'complete'), day=day, inputs=len(entries), requests=analyzer.calls,
                               pending=after['pending'], cooldown=after['cooldown'], exhausted=after['exhausted'], ambiguous=after['ambiguous'],
                               remaining=ledger.remaining(day, config['max_calls_per_day']), collection_exit=collected, assessment_exit=assessed)
            except (CollectionError, OSError, sqlite3.Error, ValueError) as exc:
                code = ('collection_error' if isinstance(exc, CollectionError) else
                        'database_error' if isinstance(exc, sqlite3.Error) else
                        'filesystem_error' if isinstance(exc, OSError) else 'validation_error')
                summary.update(status='error', operational_error=True, phase=phase, error_code=code)
            if 'telegram_config' in config and phase == 'queue_refresh':
                from .telegram_command import send_from_daily
                delivery=send_from_daily(config)
                summary['telegram']=delivery
                summary['messaging']=delivery['status']
                if delivery['status'] in {'error','unknown','failed','pending'}:
                    summary.update(status='error',operational_error=True,error_code='telegram_delivery_failed',phase='telegram')
            # Durable reservations, not a volatile counter, survive errors after dispatch.
            summary['requests'] = ledger.conn.execute(
                'SELECT count(*) FROM daily_reservations WHERE run_id=?', (run_id,)).fetchone()[0]
            ledger.finish_run(run_id, summary['status'], summary)
            _log(config['log'], summary)
        print(json.dumps(summary, ensure_ascii=False))
        return 1 if summary['status'] == 'error' else 0
    except (CollectionError, OSError, sqlite3.Error, ValueError) as exc:
        # No underlying network/credential exceptions or source text enter logs.
        message = str(exc) if isinstance(exc, CollectionError) else '일일 실행 파일/DB 설정을 확인하세요.'
        print(terminal_text(message), file=sys.stderr); return 1
