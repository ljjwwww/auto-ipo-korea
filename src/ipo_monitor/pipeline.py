"""Manual RSS -> semantic analysis -> optional public-body analysis."""
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo

from . import terminal_text
from .extraction import ExtractionError, digest, source_input, open_database
from .semantic import AIBackend, FixtureBackend, needs_body, validate
from .body import fetch_body


def now():
    return datetime.now(ZoneInfo('Asia/Seoul')).isoformat()


def schema(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS semantic_runs (
      id INTEGER PRIMARY KEY, feed_id TEXT NOT NULL, entry_id TEXT NOT NULL,
      phase TEXT NOT NULL, source_hash TEXT NOT NULL, backend_key TEXT NOT NULL,
      status TEXT NOT NULL, result TEXT, error TEXT, attempts INTEGER NOT NULL,
      updated_at TEXT NOT NULL,
      UNIQUE(feed_id,entry_id,phase,source_hash,backend_key));
    CREATE TABLE IF NOT EXISTS body_fetch_errors (
      feed_id TEXT, entry_id TEXT, rss_hash TEXT, error TEXT, updated_at TEXT,
      PRIMARY KEY(feed_id,entry_id,rss_hash));
    CREATE TABLE IF NOT EXISTS article_bodies (
      feed_id TEXT NOT NULL, entry_id TEXT NOT NULL, rss_hash TEXT NOT NULL,
      source_url TEXT NOT NULL, text TEXT NOT NULL, content_hash TEXT NOT NULL,
      fetched_at TEXT NOT NULL, PRIMARY KEY(feed_id,entry_id,rss_hash));
    ''')


def analyze(args):
    backend = FixtureBackend(args.fixture) if args.backend == 'fixture' else AIBackend(args.model, args.allow_api)
    if args.backend == 'fixture' and not args.body_fixture:
        raise ExtractionError('body_fixture_required_for_offline_mode')
    bodies = None
    if args.body_fixture:
        try:
            bodies = json.loads(args.body_fixture.read_text(encoding='utf-8'))
            if not isinstance(bodies, dict):
                raise ValueError
        except (OSError, ValueError):
            raise ExtractionError('body_fixture_invalid') from None
    calls = reused = failed = 0
    with closing(open_database(args.db)) as conn:
        schema(conn)
        posts = conn.execute('SELECT * FROM posts ORDER BY feed_id,entry_id LIMIT ?', (args.limit if args.limit is not None else -1,)).fetchall()
        for post in posts:
            # Serialize one article across overlapping processes, including remote calls.
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                current = conn.execute('SELECT * FROM posts WHERE feed_id=? AND entry_id=?',
                                       (post['feed_id'],post['entry_id'])).fetchone()
                source = source_input(current)
                ident = (post['feed_id'], post['entry_id'])
                print()
                print(terminal_text(f"{source['title']} | {source['published_at'] or source['published_raw'] or '게시 시각 없음'}"))
                print(terminal_text(source['link']))

                def run(phase, data):
                    nonlocal calls, reused, failed
                    key = (*ident, phase, digest(data), backend.cache_key(data))
                    prior = conn.execute('''SELECT * FROM semantic_runs WHERE feed_id=? AND entry_id=?
                        AND phase=? AND source_hash=? AND backend_key=?''',key).fetchone()
                    if prior and prior['status'] == 'success':
                        conn.execute('UPDATE semantic_runs SET updated_at=? WHERE id=?',(now(),prior['id']))
                        reused += 1
                        return json.loads(prior['result'])
                    calls += 1
                    result, error = None, None
                    try:
                        result = validate(backend.extract(data),data)
                    except ExtractionError as exc:
                        error = str(exc)
                        failed += 1
                    conn.execute('''INSERT INTO semantic_runs
                        (feed_id,entry_id,phase,source_hash,backend_key,status,result,error,attempts,updated_at)
                        VALUES (?,?,?,?,?,?,?,?,1,?) ON CONFLICT(feed_id,entry_id,phase,source_hash,backend_key)
                        DO UPDATE SET status=excluded.status,result=excluded.result,error=excluded.error,
                        attempts=attempts+1,updated_at=excluded.updated_at''',
                        (*key,'error' if error else 'success',json.dumps(result,ensure_ascii=False) if result else None,error,now()))
                    if error:
                        print(f'{phase} 분석 실패 · 재처리 필요: {error}')
                    return result

                def show(phase,result):
                    if result is None:
                        return
                    labels = {'ipo':'공모주','property':'부동산','other':'기타','uncertain':'유형/정보 불확실'}
                    print(f"[{phase}] {labels[result['category']]}")
                    for c in result['candidates']:
                        print(terminal_text(f"  {c['name']} | {c['opinion']} | {c['stage']} | 근거: {c['evidence']}"))

                rss = run('RSS',source)
                show('RSS',rss)
                if rss is None or not needs_body(rss):
                    continue
                print('본문 추가 확인 필요')
                body = conn.execute('SELECT * FROM article_bodies WHERE feed_id=? AND entry_id=? AND rss_hash=?',
                                    (*ident,digest(source))).fetchone()
                fetch_error = conn.execute('SELECT error FROM body_fetch_errors WHERE feed_id=? AND entry_id=? AND rss_hash=?',(*ident,digest(source))).fetchone()
                if body is None or args.refresh_body or fetch_error:
                    try:
                        if bodies is not None:
                            text = bodies.get(source['link'])
                            if not isinstance(text,str) or not text.strip():
                                raise ExtractionError('body_fixture_missing')
                            url = source['link']
                        else:
                            url,text = fetch_body(source['link'])
                        conn.execute('''INSERT INTO article_bodies VALUES (?,?,?,?,?,?,?)
                            ON CONFLICT(feed_id,entry_id,rss_hash) DO UPDATE SET source_url=excluded.source_url,
                            text=excluded.text,content_hash=excluded.content_hash,fetched_at=excluded.fetched_at''',
                            (*ident,digest(source),url,text,digest(text),now()))
                        conn.execute('DELETE FROM body_fetch_errors WHERE feed_id=? AND entry_id=? AND rss_hash=?',(*ident,digest(source)))
                    except ExtractionError as exc:
                        conn.execute('INSERT OR REPLACE INTO body_fetch_errors VALUES (?,?,?,?,?)',(*ident,digest(source),str(exc),now()))
                        failed += 1
                        print(f'본문 확인 실패 · 판단 보류 (재처리 가능): {exc}')
                        continue
                else:
                    text = body['text']
                full = {**source,'body':text}
                result = run('본문',full)
                show('본문',result)
                if result is None or needs_body(result):
                    print('최종 확인 필요: 본문에서도 불확실하거나 분석 실패. 미참여로 간주하지 않습니다.')
    print(f'AI 추출 호출: {calls}건, 재사용: {reused}건, 실패: {failed}건')
    if args.backend == 'fixture':
        print('테스트 고정 응답 · 실제 AI 분석 아님')
    return int(bool(failed))


def command(args):
    try:
        if args.limit is not None and args.limit <= 0:
            raise ExtractionError('limit_must_be_positive')
        if args.backend == 'fixture' and args.fixture is None:
            raise ExtractionError('fixture_required')
        if not args.skip_collect:
            from . import main
            if main(['collect','--config',str(args.config),'--db',str(args.db)]):
                return 1
        return analyze(args)
    except ExtractionError as exc:
        print(f'파이프라인 실패: {exc}',file=sys.stderr)
        return 1
    except (OSError,sqlite3.Error):
        print('파이프라인 저장 실패: DB 경로·권한·잠금을 확인하세요.',file=sys.stderr)
        return 1


def list_results(db_path):
    """Query current-input phase results without creating an AI backend."""
    try:
        with closing(open_database(db_path)) as conn:
            schema(conn)
            for post in conn.execute('SELECT * FROM posts ORDER BY feed_id,entry_id').fetchall():
                source = source_input(post)
                ident = (post['feed_id'],post['entry_id'])
                print(terminal_text(f"{source['title']} | {source['published_at'] or source['published_raw'] or '게시 시각 없음'} | {source['link']}"))
                body = conn.execute('SELECT * FROM article_bodies WHERE feed_id=? AND entry_id=? AND rss_hash=?',(*ident,digest(source))).fetchone()
                error = conn.execute('SELECT error FROM body_fetch_errors WHERE feed_id=? AND entry_id=? AND rss_hash=?',(*ident,digest(source))).fetchone()
                for phase,data in [('RSS',source),('본문',{**source,'body':body['text']} if body else None)]:
                    if phase == '본문' and error:
                        print(f'[본문] 수집 실패 · 판단 보류: {error[0]}')
                        continue
                    if data is None:
                        continue
                    run = conn.execute('''SELECT * FROM semantic_runs WHERE feed_id=? AND entry_id=?
                        AND phase=? AND source_hash=? ORDER BY updated_at DESC,id DESC LIMIT 1''',(*ident,phase,digest(data))).fetchone()
                    if not run:
                        print(f'[{phase}] 미분석 또는 입력 변경')
                    elif run['status']=='error':
                        print(f"[{phase}] 분석 실패 · 재처리 필요: {run['error']}")
                    else:
                        print(f"[{phase}] {'테스트 고정 응답' if run['backend_key'].startswith('fixture') else 'AI'}")
                        print(terminal_text(run['result']))
        return 0
    except (ExtractionError,OSError,sqlite3.Error):
        print('조회 실패: 먼저 수집·분석하고 DB 경로를 확인하세요.',file=sys.stderr)
        return 1
