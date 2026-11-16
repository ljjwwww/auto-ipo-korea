"""Three-source observations, immutable change history, no semantic inference."""
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
import sys
import tomllib
from zoneinfo import ZoneInfo

from . import CollectionError, terminal_text
from .extraction import digest


def now():
    return datetime.now(ZoneInfo('Asia/Seoul')).isoformat()


def connect(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS tracked_sources (
      id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL, creator TEXT NOT NULL,
      url TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS source_snapshots (
      id INTEGER PRIMARY KEY, source_id TEXT NOT NULL REFERENCES tracked_sources(id),
      item_id TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL,
      published_raw TEXT NOT NULL, published_at TEXT, payload_json TEXT NOT NULL,
      content_hash TEXT NOT NULL, observed_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS snapshot_item ON source_snapshots(source_id,item_id,id);
    CREATE TABLE IF NOT EXISTS source_attempts (
      id INTEGER PRIMARY KEY, source_id TEXT NOT NULL REFERENCES tracked_sources(id),
      started_at TEXT NOT NULL, finished_at TEXT NOT NULL, status TEXT NOT NULL,
      error TEXT, item_count INTEGER NOT NULL, new_count INTEGER NOT NULL,
      changed_count INTEGER NOT NULL, unchanged_count INTEGER NOT NULL);
    ''')
    return conn


def load_sources(path):
    try:
        with path.open('rb') as f:
            entries = tomllib.load(f).get('sources')
        if not isinstance(entries,list) or not entries:
            raise ValueError
        sources=[];seen=set()
        for entry in entries:
            if not isinstance(entry,dict):
                raise ValueError
            source = {**entry}
            source.setdefault('type','rss')
            if (not isinstance(source['type'],str) or source['type'] not in {'rss','youtube','ipo_site'}
                or not isinstance(source.get('name'),str) or not source['name'].strip()
                or not isinstance(source.get('url'),str)
                or not source['url'].startswith(('http://','https://'))
                or type(source.get('enabled')) is not bool):
                raise ValueError
            source.setdefault('id',source['type']+':'+source['url'])
            source.setdefault('creator',source['name'])
            if (not isinstance(source['id'],str) or not source['id'].strip() or source['id'] in seen
                or not isinstance(source['creator'],str)):
                raise ValueError
            if source['type']=='youtube':
                if (not isinstance(source.get('channel_id'),str)
                    or not re.fullmatch(r'UC[A-Za-z0-9_-]{22}',source['channel_id'])
                    or type(source.get('max_videos',3)) is not int or not 1<=source.get('max_videos',3)<=50
                    or type(source.get('refresh_subtitles',False)) is not bool):
                    raise ValueError
            if 'fixture' in source and (not isinstance(source['fixture'],str) or not source['fixture']):
                raise ValueError
            seen.add(source['id']);sources.append(source)
        return sources
    except (OSError,ValueError,tomllib.TOMLDecodeError):
        raise CollectionError('출처 설정 오류: name/type/url/enabled/id와 중복·형식을 확인하세요.') from None


def validate_items(items):
    if not isinstance(items,list):
        raise CollectionError('수집 결과 형식 오류')
    seen=set()
    for item in items:
        if (not isinstance(item,dict) or any(not isinstance(item.get(k),str) for k in
             ('item_id','title','url','published_raw')) or not item['item_id'] or not item['title']
             or not isinstance(item.get('payload'),dict)
             or (item.get('published_at') is not None and not isinstance(item['published_at'],str))
             or item['item_id'] in seen):
            raise CollectionError('수집 항목 형식 또는 중복 오류')
        seen.add(item['item_id'])
        captions=item['payload'].get('captions')
        if 'captions' in item['payload'] and (not isinstance(captions,dict)
            or captions.get('status') not in {'available','missing','error'}):
            raise CollectionError('자막 상태 형식 오류')
    return items


def rss_items(source,offline,config):
    from . import read_feed,parse_feed
    target = str(config.parent/source['fixture']) if offline else source['url']
    _,data=read_feed(target)
    posts=parse_feed(data)
    # Legacy RSS collection accepts repeated GUIDs. Preserve that behavior while
    # keeping one latest observation per item in a collection batch.
    unique={p['entry_id']:p for p in posts}
    return validate_items([dict(item_id=p['entry_id'],title=p['title'],url=p['link'],
        published_raw=p['published_raw'],published_at=p['published_at'],
        payload={'summary':p['summary']}) for p in unique.values()]),posts


def gather(source,previous,offline,config):
    if source['type']=='rss':
        return rss_items(source,offline,config)
    if offline:
        try:
            items=json.loads((config.parent/source['fixture']).read_text(encoding='utf-8'))
        except (OSError,ValueError):
            raise CollectionError('오프라인 fixture 파일을 읽을 수 없습니다.') from None
        return validate_items(items),None
    if source['type']=='youtube':
        from .youtube import collect
    else:
        from .ipo_site import collect
    return validate_items(collect(source,previous)),None


def collect_sources(args):
    try:
        sources=load_sources(args.config)
        if args.source and any(x not in {s['id'] for s in sources} for x in args.source):
            raise CollectionError('지정한 출처 ID가 설정에 없습니다.')
        sources=[s for s in sources if s['enabled'] and (not args.source or s['id'] in args.source)]
        if args.offline and any('fixture' not in s for s in sources):
            raise CollectionError('오프라인 모드는 모든 활성 출처에 fixture가 필요합니다. 네트워크는 사용하지 않습니다.')
        if not sources:
            print('활성 출처가 없습니다.');return 0
        failed=0
        with closing(connect(args.db)) as conn:
            # Verify all configured IDs before mutating or fetching any source.
            for s in sources:
                old=conn.execute('SELECT * FROM tracked_sources WHERE id=?',(s['id'],)).fetchone()
                if old and (old['url']!=s['url'] or old['type']!=s['type']):
                    raise CollectionError('기존 출처 ID의 URL/type은 변경할 수 없습니다. 새 ID를 사용하세요.')
            for s in sources:
                with conn:
                    conn.execute('''INSERT INTO tracked_sources VALUES (?,?,?,?,?) ON CONFLICT(id)
                      DO UPDATE SET name=excluded.name,creator=excluded.creator''',
                      (s['id'],s['type'],s['name'],s['creator'],s['url']))
                rows=conn.execute('''SELECT a.* FROM source_snapshots a WHERE a.source_id=? AND a.id=(
                    SELECT MAX(b.id) FROM source_snapshots b WHERE b.source_id=a.source_id AND b.item_id=a.item_id)''',(s['id'],)).fetchall()
                previous={r['item_id']:json.loads(r['payload_json']) for r in rows}
                started=now();new=changed=unchanged=0;error=None;status='success';count=0
                print(terminal_text(f"출처: {s['name']} [{s['id']}]"))
                try:
                    items,posts=gather(s,previous,args.offline,args.config);count=len(items)
                    # Existing blog feeds/posts remain available to all legacy CLI commands.
                    if posts is not None:
                        from . import save_posts
                        first,added=save_posts(args.db,s['url'],posts)
                        print(f"{'기준 목록' if first else '새 글'}: {len(added)}건")
                    with conn:
                        conn.execute('BEGIN IMMEDIATE')
                        for item in items:
                            payload=json.dumps(item['payload'],ensure_ascii=False,sort_keys=True)
                            h=digest({k:item.get(k) for k in ('title','url','published_raw','published_at','payload')})
                            last=conn.execute('''SELECT content_hash FROM source_snapshots WHERE source_id=?
                              AND item_id=? ORDER BY id DESC LIMIT 1''',(s['id'],item['item_id'])).fetchone()
                            if last and last[0]==h:
                                unchanged+=1;continue
                            new+=last is None;changed+=last is not None
                            conn.execute('''INSERT INTO source_snapshots
                              (source_id,item_id,title,url,published_raw,published_at,payload_json,content_hash,observed_at)
                              VALUES (?,?,?,?,?,?,?,?,?)''',
                              (s['id'],item['item_id'],item['title'],item['url'],item['published_raw'],item.get('published_at'),payload,h,now()))
                        if any(i['payload'].get('captions',{}).get('status') in {'error','missing'} for i in items):
                            status='partial';error='일부 영상의 자막을 확보하지 못했습니다. 다음 실행에서 재시도합니다.'
                except CollectionError as exc:
                    status='error';error=str(exc)
                if status!='success':
                    failed+=1;print(terminal_text(f'수집 확인 필요: {error}'))
                with conn:
                    conn.execute('''INSERT INTO source_attempts
                      (source_id,started_at,finished_at,status,error,item_count,new_count,changed_count,unchanged_count)
                      VALUES (?,?,?,?,?,?,?,?,?)''',(s['id'],started,now(),status,error,count,new,changed,unchanged))
                print(f'관찰: 신규 {new}건, 변경 {changed}건, 동일 {unchanged}건 · {status}')
        print(f'수집 결과: {len(sources)}개 출처, 실패/부분완료 {failed}개')
        return int(bool(failed))
    except CollectionError as exc:
        print(terminal_text(str(exc)),file=sys.stderr);return 1
    except (OSError,sqlite3.Error):
        print('수집 저장 오류: DB 경로·권한·잠금을 확인하세요.',file=sys.stderr);return 1


def aliases(path,stock):
    if not path.exists():
        return [stock]
    try:
        with path.open('rb') as f:
            entries=tomllib.load(f).get('stocks',[])
        if not isinstance(entries,list):
            raise ValueError
        groups=[]
        for e in entries:
            if (not isinstance(e,dict) or not isinstance(e.get('name'),str) or not e['name'].strip()
                or not isinstance(e.get('aliases',[]),list)
                or any(not isinstance(a,str) or not a.strip() for a in e.get('aliases',[]))):
                raise ValueError
            groups.append([e['name'],*e.get('aliases',[])])
        matches=[g for g in groups if stock in g]
        return list(dict.fromkeys([stock,*[a for g in matches for a in g]]))
    except (OSError,ValueError):
        raise CollectionError('종목 별칭 설정 오류') from None


def text_match(text,terms):
    # Latin tickers require boundaries; Korean substring hits remain explicit candidates.
    return any(re.search(r'(?<![A-Za-z0-9])'+re.escape(t)+r'(?![A-Za-z0-9])',text,re.I)
               if t.isascii() else t in text for t in terms)


def query(args):
    try:
        if not args.db.is_file():
            raise CollectionError('먼저 출처를 수집하세요.')
        terms=aliases(args.aliases,args.stock) if args.stock else []
        if args.limit<=0:
            raise CollectionError('limit는 양수여야 합니다.')
        with closing(connect(args.db)) as conn:
            if args.command=='source-status':
                for s in conn.execute('SELECT * FROM tracked_sources ORDER BY id').fetchall():
                    if args.source and s['id'] not in args.source:
                        continue
                    last=conn.execute('SELECT * FROM source_attempts WHERE source_id=? ORDER BY id DESC LIMIT 1',(s['id'],)).fetchone()
                    print(terminal_text(f"{s['id']} | {s['name']} | {s['creator']} | {last['status'] if last else '미수집'} | {last['finished_at'] if last else ''}"))
                    if last and last['error']:
                        print(terminal_text(last['error']))
                return 0
            rows=conn.execute('''SELECT a.*,s.name AS source_name,s.type,s.creator FROM source_snapshots a
              JOIN tracked_sources s ON s.id=a.source_id ORDER BY a.id DESC''').fetchall()
            shown=0;seen=set()
            print('관찰 자료 조회 · 문자열 연결 후보이며 공모 건 확정/참여 판단/가중 점수가 아닙니다.')
            if terms:
                print(terminal_text('검색명·등록 별칭: '+', '.join(terms)))
            for r in rows:
                key=(r['source_id'],r['item_id'])
                if not args.history and key in seen:
                    continue
                seen.add(key)
                if args.source and r['source_id'] not in args.source:
                    continue
                payload=json.loads(r['payload_json'])
                text=r['title']+'\n'+json.dumps(payload,ensure_ascii=False)
                if terms and not text_match(text,terms):
                    continue
                print()
                print(terminal_text(f"#{r['id']} {r['title']} | {r['source_name']} ({r['creator']})"))
                print(terminal_text(f"발행: {r['published_at'] or r['published_raw'] or '미상'} | 관찰: {r['observed_at']} | {r['url']}"))
                last=conn.execute('SELECT status,finished_at FROM source_attempts WHERE source_id=? ORDER BY id DESC LIMIT 1',(r['source_id'],)).fetchone()
                if last and last['status']!='success':
                    print(terminal_text(f"주의: 마지막 출처 수집 {last['status']} ({last['finished_at']}). 아래는 마지막으로 저장된 관찰입니다."))
                print(terminal_text(json.dumps(payload,ensure_ascii=False)))
                shown+=1
                if shown>=args.limit:
                    break
            print(f'표시 {shown}건'+(' (변경 이력 포함)' if args.history else ' (항목별 최신 관찰)'))
        return 0
    except CollectionError as exc:
        print(str(exc),file=sys.stderr);return 1
    except (sqlite3.Error,OSError):
        print('조회 실패: DB 경로·권한을 확인하세요.',file=sys.stderr);return 1
