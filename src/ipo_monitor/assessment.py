"""Fixture-only cross-source analysis, experimental decisions and unsent previews."""
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo

from . import CollectionError, terminal_text
from .evidence import build_inputs, context_hash, stock_groups
from .extraction import digest
from .multisource import connect
from .decision import load_policy, evaluate, VERSION as POLICY_ENGINE
from .preview import ensure_schema as preview_schema, enqueue

VERSION='fixture-assessment-v1'
FIELDS={'opinion','assessment','evidence','stage','reason'}


def now():
    return datetime.now(ZoneInfo('Asia/Seoul')).isoformat()


def ensure_schema(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS observation_analyses (
      id INTEGER PRIMARY KEY,input_hash TEXT NOT NULL,backend_key TEXT NOT NULL,
      status TEXT NOT NULL,response_json TEXT,error TEXT,attempts INTEGER NOT NULL,
      updated_at TEXT NOT NULL,UNIQUE(input_hash,backend_key));
    CREATE TABLE IF NOT EXISTS decision_versions (
      id INTEGER PRIMARY KEY,stock TEXT NOT NULL,bundle_hash TEXT NOT NULL,
      policy_hash TEXT NOT NULL,context_hash TEXT NOT NULL,result_json TEXT NOT NULL,
      created_at TEXT NOT NULL,UNIQUE(stock,bundle_hash,policy_hash));
    CREATE TABLE IF NOT EXISTS decision_current (
      stock TEXT PRIMARY KEY,version_id INTEGER NOT NULL REFERENCES decision_versions(id));
    CREATE TABLE IF NOT EXISTS decision_transitions (
      id INTEGER PRIMARY KEY,stock TEXT NOT NULL,
      version_id INTEGER NOT NULL REFERENCES decision_versions(id),selected_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS decision_transition_current (
      stock TEXT PRIMARY KEY,transition_id INTEGER NOT NULL REFERENCES decision_transitions(id));
    CREATE TABLE IF NOT EXISTS ollama_usage (
      id INTEGER PRIMARY KEY,input_hash TEXT NOT NULL,model TEXT NOT NULL,
      input_tokens INTEGER,output_tokens INTEGER,http_status INTEGER,
      thinking_used INTEGER,recorded_at TEXT NOT NULL);
    ''')
    # Preserve previously created preview IDs when upgrading an existing DB.
    if not conn.execute('SELECT 1 FROM decision_transitions LIMIT 1').fetchone():
        versions=conn.execute('SELECT id,stock,created_at FROM decision_versions ORDER BY id').fetchall()
        conn.executemany('INSERT INTO decision_transitions VALUES (?,?,?,?)',
                         [(v['id'],v['stock'],v['id'],v['created_at']) for v in versions])
        conn.execute('''INSERT OR IGNORE INTO decision_transition_current
          SELECT stock,version_id FROM decision_current''')
    preview_schema(conn)
    conn.commit()


def validate_response(response,source):
    if not isinstance(response,dict) or set(response)!=FIELDS:
        raise CollectionError('analysis_schema_invalid')
    if any(not isinstance(v,str) for v in response.values()):
        raise CollectionError('analysis_schema_invalid')
    if (response['opinion'] not in {'참여','조건부','미참여','판단 불가'}
        or response['assessment'] not in {'positive','negative','neutral','unknown'}
        or response['stage'] not in {'planned','applied','retrospective','unknown'}
        or not response['evidence'].strip() or not 1<=len(response['evidence'])<=300 or len(response['reason'])>500):
        raise CollectionError('analysis_schema_invalid')
    if response['evidence'] not in source['content']['text']:
        raise CollectionError('analysis_evidence_not_in_source')
    return response


class FixtureAnalyzer:
    origin='fixture'
    def __init__(self,path):
        try:
            self.responses=json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(self.responses,dict):
                raise ValueError
        except (ValueError,OSError):
            raise CollectionError('analysis_fixture_invalid') from None
    def key(self,h):
        return f'fixture:{VERSION}:{digest(self.responses.get(h))}'
    def analyze(self,h,source):
        if h not in self.responses:
            raise CollectionError('analysis_fixture_missing')
        return validate_response(self.responses[h],source)


def site_rating(source):
    values=[]
    for row in source['content']['ratings']:
        stars=row.get('stars');marker=row.get('raw_marker','')
        if stars is not None and (type(stars) is not int or not 0<=stars<=3):
            raise CollectionError('rating_observation_invalid')
        if not isinstance(marker,str):
            raise CollectionError('rating_observation_invalid')
        if row.get('marker_conflict') or ('⭐' in marker and '❌' in marker):
            return None,'혼합',True
        if stars is not None or marker:
            values.append((stars,marker))
    unique=list(dict.fromkeys(values))
    # No rating is not zero. Conflicting observations are preserved, not guessed.
    if len(unique)>1:
        return None,'혼합',True
    return (*unique[0],False) if unique else (None,'',False)


class AnalysisDeferred(CollectionError):
    """Input waits in the daily queue; do not persist it as an analysis failure."""


def analyze_entry(conn,entry,analyzer):
    """Cache-or-call one common input; the caller owns the write lock/transaction."""
    source=entry['input'];h=entry['input_hash'];key=analyzer.key(h)
    # Validate deterministic source fields before dispatching any paid request.
    rating,marker,rating_conflict=site_rating(source) if source['source_type']=='ipo_site' else (None,'',False)
    ambiguous=entry.get('match_quality')=='substring_candidate'
    if ambiguous:
        key+=':unconfirmed-entity-v1'
    prior=conn.execute('SELECT * FROM observation_analyses WHERE input_hash=? AND backend_key=?',(h,key)).fetchone()
    reused=attempted=failed=deferred=False
    if prior and prior['status']=='success':
        response=json.loads(prior['response_json']);status='success';error=None;reused=True
    else:
        response=None;error=None;attempted=True
        try:
            if ambiguous:
                raise CollectionError('analysis_target_ambiguous: 부분 문자열만 일치, 종목 연결 추가 확인 필요')
            response=analyzer.analyze(h,source)
        except AnalysisDeferred:
            deferred=True;attempted=False
            error="daily_queue_pending"
        except CollectionError as exc:
            error=str(exc);failed=True
        status='unprocessed' if deferred else ('error' if error else 'success')
        if not deferred:
            conn.execute('''INSERT INTO observation_analyses
          (input_hash,backend_key,status,response_json,error,attempts,updated_at)
          VALUES (?,?,?,?,?,1,?) ON CONFLICT(input_hash,backend_key) DO UPDATE SET
          status=excluded.status,response_json=excluded.response_json,error=excluded.error,
          attempts=attempts+1,updated_at=excluded.updated_at''',
          (h,key,status,json.dumps(response,ensure_ascii=False) if response else None,error,now()))
    analysis_row=conn.execute('SELECT id FROM observation_analyses WHERE input_hash=? AND backend_key=?',(h,key)).fetchone()
    analysis_id=analysis_row[0] if analysis_row and status!='unprocessed' else None
    signal={'stock':source['stock'],'source_id':source['source_id'],'source_type':source['source_type'],
        'creator':source['creator'],'snapshot_id':entry['snapshot_id'],'observed_at':entry['observed_at'],
        'published_at':source['published_at'],'published_raw':source['published_raw'],'url':source['url'],
        'input_hash':h,'analysis_id':analysis_id,'backend_key':key,
        'source_status':entry['source_status'],'source_error':entry['source_error'],
        'match_quality':entry.get('match_quality','unknown'),
        'opinion':'판단 불가','assessment':'unknown','stage':'unknown','evidence':'','reason':'',
        **(response or {}),'rating':rating,'marker':marker,'rating_conflict':rating_conflict,
        'analysis_status':status,'error':error,'raw_ratings':source['content']['ratings'],'analysis_reason':response['reason'] if response else ''}
    # Timestamp links are grounded in stored caption segments, not invented by the model.
    signal['segments']=[s for s in source['content']['segments'] if response and response['evidence'] in s['text']]
    return signal,{'reused':reused,'call':attempted,'failure':failed}


def checkpoint_entries(conn,entries,analyzer,record_usage):
    """Commit each paid result and its usage before any decision is written.

    The per-input BEGIN IMMEDIATE is held across the network call, so an overlapping
    run blocks here and then reuses the committed success instead of paying twice.
    A later decision error can only roll back decision rows, never paid results.
    """
    signals=[];calls=reused=failures=0
    usage_seen=len(getattr(analyzer,'usage',[]) or [])
    for entry in entries:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            signal,counts=analyze_entry(conn,entry,analyzer)
            if record_usage:
                usage=getattr(analyzer,'usage',[]) or []
                for item in usage[usage_seen:]:
                    conn.execute('''INSERT INTO ollama_usage
                      (input_hash,model,input_tokens,output_tokens,http_status,thinking_used,recorded_at)
                      VALUES (?,?,?,?,?,?,?)''',
                      (item['input_hash'],item['model'],item['input_tokens'],item['output_tokens'],
                       item['http_status'],item['thinking_used'],now()))
                usage_seen=len(usage)
        signals.append(signal)
        calls+=counts['call'];reused+=counts['reused'];failures+=counts['failure']
    return signals,calls,reused,failures


def entry_state(entries):
    """Provenance identity used to detect a collect that landed mid-assessment."""
    return tuple((e['input_hash'],e['snapshot_id'],e.get('match_quality'),e['source_status'],e['source_error'])
                 for e in entries)


def run_assessment(args, analyzer_override=None):
    try:
        if not args.db.is_file():
            raise CollectionError('먼저 출처를 수집하세요.')
        policy=load_policy(args.policy)
        if analyzer_override is not None:
            analyzer=analyzer_override
        elif args.backend=='fixture':
            if args.fixture is None:
                raise CollectionError('fixture_required')
            analyzer=FixtureAnalyzer(args.fixture)
        else:
            if not args.allow_api:
                raise CollectionError('ollama_requires_allow_api')
            if not args.stock:
                raise CollectionError('ollama_requires_explicit_stock')
            if not args.model or args.fixture is not None:
                raise CollectionError('ollama_requires_model_and_no_fixture')
            from .credentials import read_key
            from .ollama import OllamaAnalyzer
            analyzer=OllamaAnalyzer(args.model,args.allow_api,args.max_calls,read_key())
        with closing(connect(args.db)) as conn:
            ensure_schema(conn)
            record_usage=args.backend=='ollama'
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                entries=build_inputs(conn,args.aliases,args.stock)
                if not entries:
                    raise CollectionError('no_matching_analysis_inputs')
            if args.backend=='ollama' and not getattr(analyzer,'can_defer',False):
                required=sum(not conn.execute('''SELECT 1 FROM observation_analyses
                  WHERE input_hash=? AND backend_key=? AND status='success' ''',
                  (e['input_hash'],analyzer.key(e['input_hash']))).fetchone()
                  for e in entries if e.get('match_quality')!='substring_candidate')
                if required>args.max_calls:
                    raise CollectionError(f'ollama_call_budget_exceeded: 필요한 요청 {required}건 > 설정 {args.max_calls}건. 호출하지 않았습니다.')
            signals,calls,reused,failures=checkpoint_entries(conn,entries,analyzer,record_usage)
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                # No implicit retry: a collect that landed during checkpoints requires an explicit rerun.
                if entry_state(build_inputs(conn,args.aliases,args.stock))!=entry_state(entries):
                    raise CollectionError('assessment_inputs_changed: 분석 중 수집 결과가 변경되었습니다. 저장한 분석은 유지되며 다시 실행해야 판단을 갱신합니다.')
                stock_names=sorted({s['stock'] for s in signals})
                decisions_created=previews_created=transitions_created=0
                for stock in stock_names:
                    parts=[s for s in signals if s['stock']==stock]
                    context=context_hash([e for e in entries if e['input']['stock']==stock])
                    bundle=digest({'version':VERSION,'context':context,'signals':parts})
                    policy_hash=digest({'engine':POLICY_ENGINE,'policy':policy})
                    prior=conn.execute('SELECT * FROM decision_versions WHERE stock=? AND bundle_hash=? AND policy_hash=?',
                                       (stock,bundle,policy_hash)).fetchone()
                    if prior:
                        decision_id=prior['id'];result=json.loads(prior['result_json'])
                    else:
                        result=evaluate(parts,policy)
                        result['analysis_issues']=[{'source_id':s['source_id'],'error':s['error']} for s in result['contributions'] if s['selected'] and s['analysis_status']!='success']
                        if result['analysis_issues']:
                            result['status']='추가 확인'
                        result.update({'stock':stock,'is_test':True,'analysis_origin':analyzer.origin,'input_context':context,'policy_hash':policy_hash,'policy_snapshot':policy,'engine_version':POLICY_ENGINE})
                        if any(s['rating_conflict'] for s in parts):
                            result['status']='추가 확인';result['rating_conflict']=True
                        cursor=conn.execute('''INSERT INTO decision_versions
                          (stock,bundle_hash,policy_hash,context_hash,result_json,created_at) VALUES (?,?,?,?,?,?)''',
                          (stock,bundle,policy_hash,context,json.dumps(result,ensure_ascii=False),now()))
                        decision_id=cursor.lastrowid;decisions_created+=1
                    conn.execute('INSERT INTO decision_current VALUES (?,?) ON CONFLICT(stock) DO UPDATE SET version_id=excluded.version_id',
                                 (stock,decision_id))
                    active=conn.execute('''SELECT t.* FROM decision_transitions t
                      JOIN decision_transition_current c ON c.transition_id=t.id WHERE c.stock=?''',(stock,)).fetchone()
                    if active and active['version_id']==decision_id:
                        transition_id=active['id']
                    else:
                        transition_id=conn.execute('INSERT INTO decision_transitions (stock,version_id,selected_at) VALUES (?,?,?)',
                                                   (stock,decision_id,now())).lastrowid
                        conn.execute('INSERT INTO decision_transition_current VALUES (?,?) ON CONFLICT(stock) DO UPDATE SET transition_id=excluded.transition_id',
                                     (stock,transition_id))
                        transitions_created+=1
                    _,created=enqueue(conn,transition_id,stock,result,args.channel,args.destination,is_test=True)
                    previews_created+=created
                    print(terminal_text(f"[테스트] {stock}: {result['status']} | 점수 {result['score']} | 자료커버리지 {result['coverage']}"))
        print(f"{'고정 응답' if analyzer.origin=='fixture' else 'Ollama 분석'} 호출 {calls}건 · 재사용 {reused}건 · 분석 실패 {failures}건")
        print(f'판단 버전 신규 {decisions_created}건 · 상태 전이 신규 {transitions_created}건 · 미발송 미리보기 신규 {previews_created}건')
        if analyzer.origin=='fixture':
            print('실제 AI/메신저 호출 없음. 고정 응답과 미검증 실험 정책 결과입니다.')
        else:
            print(f'실제 Ollama 요청 {analyzer.calls}건. 메신저 전송 없음. 미검증 실험 정책 결과입니다.')
        return int(bool(failures))
    except (CollectionError,ValueError) as exc:
        print(terminal_text(f'분석 준비/검증 실패: {exc}'),file=sys.stderr);return 1
    except (OSError,sqlite3.Error):
        print('분석 저장 실패: DB·파일 경로·권한을 확인하세요.',file=sys.stderr);return 1


def query_assessment(args):
    try:
        if not args.db.is_file():
            raise CollectionError('먼저 출처를 수집하세요.')
        with closing(connect(args.db)) as conn:
            ensure_schema(conn)
            entries=build_inputs(conn,args.aliases,args.stock)
            if args.command=='evidence-inputs':
                data=json.dumps(entries,ensure_ascii=False,indent=2)
                if args.output:
                    # Prevent accidentally publishing source text with an export into the repo.
                    root=Path('data').resolve();target=args.output.resolve()
                    if not target.is_relative_to(root):
                        raise CollectionError('원문 포함 입력 내보내기는 프로젝트 data/ 아래만 허용합니다.')
                    target.parent.mkdir(parents=True,exist_ok=True);target.write_text(data+'\n',encoding='utf-8')
                    print(f'공통 분석 입력 {len(entries)}건 저장 (AI 호출 없음)')
                else:
                    print('\n'.join(terminal_text(line) for line in data.splitlines()))
                return 0
            current={stock:context_hash([e for e in entries if e['input']['stock']==stock])
                     for stock in {e['input']['stock'] for e in entries}}
            active_policy_hash=digest({'engine':POLICY_ENGINE,'policy':load_policy(args.policy)})
            groups=stock_groups(args.aliases)
            requested={next((name for name,terms in groups.items() if value.casefold() in {t.casefold() for t in terms}),value)
                       for value in (args.stock or [])}
            if args.command=='decisions':
                if args.history:
                    rows=conn.execute('''SELECT v.*,t.id AS transition_id,t.selected_at FROM decision_versions v
                      JOIN decision_transitions t ON t.version_id=v.id ORDER BY t.id DESC''').fetchall()
                else:
                    rows=conn.execute('''SELECT v.*,t.id AS transition_id,t.selected_at FROM decision_versions v
                      JOIN decision_transitions t ON t.version_id=v.id JOIN decision_transition_current c
                      ON c.transition_id=t.id ORDER BY t.id DESC''').fetchall()
                for row in rows:
                    if args.stock and row['stock'] not in requested:
                        continue
                    stale=current.get(row['stock'])!=row['context_hash'] or row['policy_hash']!=active_policy_hash
                    print(terminal_text(f"[테스트 판단 #{row['id']} / 전이 #{row['transition_id']} {row['selected_at']}] {row['stock']} | {'입력 변경/자료 부재 · 재분석 필요' if stale else '현재 입력 일치'}"))
                    print(terminal_text(row['result_json']))
                if not rows:
                    print('저장된 판단 없음')
                return 0
            rows=conn.execute('''SELECT o.*,v.context_hash,v.policy_hash,c.transition_id AS current_id FROM notification_outbox o
              JOIN decision_transitions t ON t.id=o.decision_id JOIN decision_versions v ON v.id=t.version_id
              LEFT JOIN decision_transition_current c ON c.stock=o.stock ORDER BY o.id''').fetchall()
            shown=0
            for row in rows:
                if args.stock and row['stock'] not in requested:
                    continue
                stale=current.get(row['stock'])!=row['context_hash'] or row['policy_hash']!=active_policy_hash or row['current_id']!=row['decision_id']
                if stale and not args.history:
                    continue
                print(terminal_text(f"미발송 #{row['id']} | {row['channel']} | {row['destination']}"))
                if stale:
                    print('과거/입력 변경 미리보기 · 전송 대상 아님')
                print('\n'.join(terminal_text(line) for line in row['message'].splitlines()));shown+=1
            print(f'미발송 미리보기 {shown}건 · 실제 전송 없음')
            return 0
    except (CollectionError,ValueError) as exc:
        print(terminal_text(f'조회 실패: {exc}'),file=sys.stderr);return 1
    except (sqlite3.Error,OSError):
        print('조회 실패: DB·파일 경로·권한을 확인하세요.',file=sys.stderr);return 1
