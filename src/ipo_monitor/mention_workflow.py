"""Monthly catalog -> new source mention -> bounded analysis -> event notification."""
from contextlib import chdir, closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
import time
from types import SimpleNamespace

from . import CollectionError
from .assessment import AnalysisDeferred, FixtureAnalyzer
from .daily_state import DailyLedger, file_lock
from .event_validation import validate_event_response
from .extraction import digest
from .multisource import collect_sources, connect
from .ollama import INSTRUCTIONS, VERSION, OllamaAnalyzer, request_settings

EVENT_VERSION='event-summary-v2'
EVENT_INSTRUCTIONS=INSTRUCTIONS+'''
추가 작업: reason 필드는 이 새 글/영상이 해당 종목에 관해 전달하는 핵심을 한국어 한 문장,
180자 이내로 요약한다. 개인 청약 의사만 반복하지 말고 원문에 있는 일정·공모가·수요예측·
평가 변화·참여 의견 중 실제로 언급된 핵심을 요약한다. 없는 숫자·날짜·추천을 만들지 않는다.
reason에 쓴 공모가·경쟁률·퍼센트·일정은 evidence 또는 원문 문맥에 그대로 있는 값만 쓴다.
참여/조건부/미참여는 작성자가 원문에서 직접 밝힌 경우만 고르고, 별점·경쟁률·제목만으로
참여 의사를 만들지 않는다. stage=applied는 신청/청약을 이미 했다는 명시적 표현이 있을 때만
쓴다. evidence는 evidence_choices 중 대상 종목/별칭이 들어 있는 구절 하나를 글자 그대로
복사한다. 선택지에 없거나 종목이 없는 구절, 직접 만든 인용은 허용되지 않는다.
참여 의사가 불명확하면 opinion은 판단 불가로 두되 원문에 있는 사실은 reason에 요약한다.
자막/설명 없이 제목만 있으면 제목에서 언급됨과 상세 내용 미확보를 분명히 밝힌다.
'''


class EventAnalyzer(OllamaAnalyzer):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self._settings['instructions']=EVENT_INSTRUCTIONS

    def key(self,input_hash):
        return f'ollama:{VERSION}:{EVENT_VERSION}:{self.model}:{digest(self._settings)}'


def event_cache_key(model):
    settings=request_settings();settings['instructions']=EVENT_INSTRUCTIONS
    return f'ollama:{VERSION}:{EVENT_VERSION}:{model}:{digest(settings)}'


def _safe_code(exc,fallback):
    code=str(exc)
    return code if re.fullmatch(r'[a-z0-9_]{1,80}',code) else fallback


def _controls(conn):
    """Read the parent-owned optional control table; never create it here."""
    tables={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'mention_event_controls' not in tables:
        return {}
    columns={row[1] for row in conn.execute('PRAGMA table_info(mention_event_controls)')}
    if not {'event_id','closed','analysis_retry_at'}.issubset(columns):
        return {}
    return {row['event_id']:row for row in conn.execute('SELECT * FROM mention_event_controls')}


def _attempts(ledger,input_hash,model,retry_at):
    query='''SELECT r.started_at FROM daily_reservations a JOIN daily_runs r ON r.id=a.run_id
        WHERE a.input_hash=? AND a.model=?'''
    params=[input_hash,model]
    # An explicit operator retry starts a new attempt epoch without deleting quota history.
    if isinstance(retry_at,str) and retry_at:
        query+=' AND r.started_at>=?'
        params.append(retry_at)
    return ledger.conn.execute(query+' ORDER BY a.id DESC',params).fetchall()


def now_for_queue():
    from .daily import now
    return now().astimezone(timezone.utc)


def _queue_counts(config,ledger,day,rows,active,controls):
    counts={'active_pending':0,'cooldown':0,'exhausted':0,'expired':0,'closed':0}
    eligible=[];open_ids=set()
    for row in rows:
        control=controls.get(row['id'])
        if control is not None and control['closed']:
            counts['closed']+=1;continue
        end=active.get(row['stock'])
        if end is None or end<day:
            counts['expired']+=1;continue
        open_ids.add(row['id'])
        retry_at=control['analysis_retry_at'] if control is not None else None
        attempts=_attempts(ledger,row['input_hash'],config['model'],retry_at)
        if len(attempts)>=config['max_attempts']:
            counts['exhausted']+=1;continue
        if attempts and now_for_queue()-datetime.fromisoformat(attempts[0][0])<timedelta(hours=config['retry_hours']):
            counts['cooldown']+=1;continue
        counts['active_pending']+=1
        eligible.append(row)
    return counts,eligible,open_ids


def analyze_pending(config,state,ledger,run_id,day):
    from .daily import DailyAnalyzer
    state.execute('''CREATE TABLE IF NOT EXISTS mention_analysis_cache (
        input_hash TEXT NOT NULL,backend_key TEXT NOT NULL,response_json TEXT NOT NULL,
        PRIMARY KEY(input_hash,backend_key))''');state.commit()
    rows=state.execute("SELECT * FROM mention_events WHERE analysis_status!='success' ORDER BY id").fetchall()
    fixture=FixtureAnalyzer(config['fixture']) if config['backend']=='fixture' else None
    key_for=lambda h: event_cache_key(config['model']) if fixture is None else fixture.key(h)
    active={}
    for item in state.execute('SELECT stock,payload_json FROM mention_catalog WHERE month=?',(day[:7],)):
        try:
            end=json.loads(item['payload_json'])['end']
        except (ValueError,TypeError,KeyError):
            continue
        if isinstance(end,str):
            active[item['stock']]=end
    controls=_controls(state)
    _,selected,open_ids=_queue_counts(config,ledger,day,rows,active,controls)
    selected_ids={row['id'] for row in selected}
    reused=errors=0;error_codes={}
    def failed_event(row,code):
        nonlocal errors
        with state:
            state.execute("UPDATE mention_events SET analysis_status='error',attempts=attempts+1,error_code=? WHERE id=?",(code,row['id']))
        errors+=1;error_codes[code]=error_codes.get(code,0)+1
    ready=[]
    for row in rows:
        if row['id'] not in open_ids:
            continue
        key=key_for(row['input_hash'])
        cached=state.execute('SELECT response_json FROM mention_analysis_cache WHERE input_hash=? AND backend_key=?',(row['input_hash'],key)).fetchone()
        if cached:
            try:
                response=validate_event_response(json.loads(cached[0]),json.loads(row['input_json']))
            except (CollectionError,ValueError,KeyError,TypeError) as exc:
                # Never promote an invalid cached summary; drop it so a later run can retry.
                with state:
                    state.execute('DELETE FROM mention_analysis_cache WHERE input_hash=? AND backend_key=?',(row['input_hash'],key))
                failed_event(row,_safe_code(exc,'event_cached_invalid'))
                continue
            with state:
                state.execute("UPDATE mention_events SET analysis_status='success',analysis_json=?,analysis_origin=?,error_code=NULL WHERE id=?",(cached[0],config['backend'],row['id']))
            reused+=1
            continue
        if row['id'] in selected_ids:
            ready.append(row)
    selected=ready[:ledger.remaining(day,config['max_calls_per_day'])]
    analyzer=DailyAnalyzer(config,[r['input_hash'] for r in selected],ledger,run_id,day)
    # Credentials are not read when the run has no new analyzable events.
    if selected and time.monotonic()<config.get('_deadline',float('inf')):
        if config['backend']=='ollama':
            from .credentials import read_key
            with chdir(config['root']):
                secret=read_key()
            analyzer.delegate=EventAnalyzer(config['model'],True,config['max_calls_per_day'],secret)
        else:
            analyzer.delegate=FixtureAnalyzer(config['fixture'])
        for row in selected:
            source=json.loads(row['input_json'])
            try:
                response=analyzer.analyze(row['input_hash'],source)
                response=validate_event_response(response,source)
            except AnalysisDeferred:
                break
            except (CollectionError,ValueError,KeyError,TypeError) as exc:
                failed_event(row,_safe_code(exc,'mention_analysis_failed'))
            else:
                encoded=json.dumps(response,ensure_ascii=False)
                with state:
                    state.execute("UPDATE mention_events SET analysis_status='success',attempts=attempts+1,analysis_json=?,analysis_origin=?,error_code=NULL WHERE id=?",(encoded,config['backend'],row['id']))
                    state.execute('INSERT OR REPLACE INTO mention_analysis_cache VALUES (?,?,?)',(row['input_hash'],key_for(row['input_hash']),encoded))
    return {'requests':analyzer.calls,'reused':reused,'analysis_errors':errors,
            'analysis_error_codes':dict(sorted(error_codes.items())),
            **_finish_counts(config,state,ledger,day,active,controls)}


def _finish_counts(config,state,ledger,day,active,controls):
    """Recount the queue after this run so just-analyzed events are not pending."""
    rows=state.execute("SELECT * FROM mention_events WHERE analysis_status!='success' ORDER BY id").fetchall()
    counts,_,_=_queue_counts(config,ledger,day,rows,active,controls)
    counts['pending']=counts['active_pending']+counts['cooldown']+counts['exhausted']
    return counts


def deliver(config,dry_run=False):
    from .telegram_command import load_config
    from .mention_delivery import deliver_events
    telegram=load_config(config['telegram_config'])
    if dry_run:
        return deliver_events(config['event_db'],dry_run=True)
    if not telegram['enabled']:
        return {'status':'disabled','sent':0}
    if config['backend']!='ollama':
        return {'status':'fixture_blocked','sent':0}
    from .telegram_credentials import read_credentials
    with chdir(telegram['root']):
        credential=read_credentials()
    return deliver_events(config['event_db'],credential,allow_send=True)


def command(config,args):
    from .daily import now,_log
    from .mentions import discover,ensure_schema
    if args.dry_run:
        result=deliver(config,dry_run=True)
        print(json.dumps({'status':'dry_run','requests':0,'telegram':result},ensure_ascii=False));return 0
    if not config['enabled']:
        print(json.dumps({'status':'disabled','requests':0}));return 0
    if config['backend']=='ollama' and not args.allow_api:
        raise CollectionError('mention_workflow_requires_allow_api')
    summary={'workflow':'mention_events','status':'error','requests':0,
             'messaging':'not_sent','phase':'collection','error_code':None}
    with file_lock(config['ledger'].with_suffix('.lock')),DailyLedger(config['ledger']) as ledger:
        current=now();day=current.date().isoformat();run=ledger.start_run(day)
        config['_deadline']=time.monotonic()+60*config['max_runtime_minutes']
        phase='collection'
        try:
            collected=collect_sources(SimpleNamespace(config=config['sources'],db=config['db'],offline=config['offline'],source=None))
            phase='catalog'
            config['event_db'].parent.mkdir(parents=True,exist_ok=True)
            with closing(sqlite3.connect(config['event_db'])) as state,closing(connect(config['db'])) as sources:
                state.row_factory=sqlite3.Row;ensure_schema(state);config['event_db'].chmod(0o600)
                found=discover(sources,config['aliases'],state,current.date())
                summary['discovery']=found
                if found.get('stale_catalog'):
                    raise CollectionError('mention_catalog_stale')
                phase='analysis'
                summary.update(analyze_pending(config,state,ledger,run,day))
            phase='delivery'
            result=deliver(config)
            summary.update(telegram=result,messaging=result['status'],collection_exit=collected,
                           remaining=ledger.remaining(day,config['max_calls_per_day']))
            bad=bool(collected or summary['analysis_errors'] or result['status'] in {'failed','unknown','error','stale_catalog'})
            summary['status']='error' if bad else ('partial' if summary['pending'] else 'success')
            if collected:
                phase='collection';summary['error_code']='mention_collection_failed'
            elif summary['analysis_errors']:
                phase='analysis';summary['error_code']='mention_analysis_failed'
            elif result['status'] in {'failed','unknown','error','stale_catalog'}:
                phase='delivery';summary['error_code']=f'mention_delivery_{result["status"]}'
        except (CollectionError,OSError,sqlite3.Error,ValueError,KeyError) as exc:
            if isinstance(exc,CollectionError):
                code=_safe_code(exc,'mention_workflow_failed')
            elif isinstance(exc,sqlite3.Error):
                code='mention_database_failed'
            elif isinstance(exc,OSError):
                code='mention_filesystem_failed'
            else:
                code='mention_validation_failed'
            summary.update(status='error',error_code=code)
        summary['phase']=phase
        summary['requests']=ledger.conn.execute('SELECT count(*) FROM daily_reservations WHERE run_id=?',(run,)).fetchone()[0]
        ledger.finish_run(run,summary['status'],summary);_log(config['log'],summary)
    print(json.dumps(summary,ensure_ascii=False))
    return int(summary['status']=='error')
