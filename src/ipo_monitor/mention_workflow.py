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
from .assessment import AnalysisDeferred, FixtureAnalyzer, validate_response
from .daily_state import DailyLedger, file_lock
from .extraction import digest
from .multisource import collect_sources, connect
from .ollama import INSTRUCTIONS, VERSION, OllamaAnalyzer, request_settings

EVENT_INSTRUCTIONS=INSTRUCTIONS+'''
추가 작업: reason 필드는 이 새 글/영상이 해당 종목에 관해 전달하는 핵심을 한국어 한 문장,
180자 이내로 요약한다. 개인 청약 의사만 반복하지 말고 원문에 있는 일정·공모가·수요예측·
평가 변화·참여 의견 중 실제로 언급된 핵심을 요약한다. 없는 숫자나 추천을 만들지 않는다.
참여 의사가 불명확하면 opinion은 판단 불가로 두되 원문에 있는 사실은 reason에 요약한다.
자막/설명 없이 제목만 있으면 제목에서 언급됨과 상세 내용 미확보를 분명히 밝힌다.
'''


class EventAnalyzer(OllamaAnalyzer):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self._settings['instructions']=EVENT_INSTRUCTIONS


def event_cache_key(model):
    settings=request_settings();settings['instructions']=EVENT_INSTRUCTIONS
    return f'ollama:{VERSION}:{model}:{digest(settings)}'


def analyze_pending(config,state,ledger,run_id,day):
    from .daily import DailyAnalyzer, now
    state.execute('''CREATE TABLE IF NOT EXISTS mention_analysis_cache (
        input_hash TEXT NOT NULL,backend_key TEXT NOT NULL,response_json TEXT NOT NULL,
        PRIMARY KEY(input_hash,backend_key))''');state.commit()
    rows=state.execute("SELECT * FROM mention_events WHERE analysis_status!='success' ORDER BY id").fetchall()
    fixture=FixtureAnalyzer(config['fixture']) if config['backend']=='fixture' else None
    key_for=lambda h: event_cache_key(config['model']) if fixture is None else fixture.key(h)
    selected=[];reused=errors=0
    active={r['stock'] for r in state.execute('SELECT stock,payload_json FROM mention_catalog WHERE month=?',(day[:7],))
            if json.loads(r['payload_json']).get('end','')>=day}
    for row in rows:
        if row['stock'] not in active:
            continue
        key=key_for(row['input_hash'])
        cached=state.execute('SELECT response_json FROM mention_analysis_cache WHERE input_hash=? AND backend_key=?',(row['input_hash'],key)).fetchone()
        if cached:
            validate_response(json.loads(cached[0]),json.loads(row['input_json']))
            with state:
                state.execute("UPDATE mention_events SET analysis_status='success',analysis_json=?,analysis_origin=?,error_code=NULL WHERE id=?",(cached[0],config['backend'],row['id']))
            reused+=1;continue
        attempts=ledger.conn.execute('''SELECT r.started_at FROM daily_reservations a JOIN daily_runs r ON r.id=a.run_id
            WHERE a.input_hash=? AND a.model=? ORDER BY a.id DESC''',(row['input_hash'],config['model'])).fetchall()
        if len(attempts)>=config['max_attempts']:
            continue
        if attempts and now().astimezone(timezone.utc)-datetime.fromisoformat(attempts[0][0])<timedelta(hours=config['retry_hours']):
            continue
        selected.append(row)
    selected=selected[:ledger.remaining(day,config['max_calls_per_day'])]
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
            try:
                response=analyzer.analyze(row['input_hash'],json.loads(row['input_json']))
                response=validate_response(response,json.loads(row['input_json']))
            except AnalysisDeferred:
                break
            except CollectionError as exc:
                code=str(exc) if re.fullmatch(r'[a-z0-9_]+',str(exc)) else 'mention_analysis_failed'
                with state:
                    state.execute("UPDATE mention_events SET analysis_status='error',attempts=attempts+1,error_code=? WHERE id=?",(code,row['id']))
                errors+=1
            else:
                encoded=json.dumps(response,ensure_ascii=False)
                with state:
                    state.execute("UPDATE mention_events SET analysis_status='success',attempts=attempts+1,analysis_json=?,analysis_origin=?,error_code=NULL WHERE id=?",(encoded,config['backend'],row['id']))
                    state.execute('INSERT OR REPLACE INTO mention_analysis_cache VALUES (?,?,?)',(row['input_hash'],key_for(row['input_hash']),encoded))
    return {'requests':analyzer.calls,'reused':reused,'analysis_errors':errors,
            'pending':state.execute("SELECT count(*) FROM mention_events WHERE analysis_status!='success'").fetchone()[0]}


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
    summary={'workflow':'mention_events','status':'error','requests':0,'messaging':'not_sent'}
    with file_lock(config['ledger'].with_suffix('.lock')),DailyLedger(config['ledger']) as ledger:
        current=now();day=current.date().isoformat();run=ledger.start_run(day)
        config['_deadline']=time.monotonic()+60*config['max_runtime_minutes']
        try:
            collected=collect_sources(SimpleNamespace(config=config['sources'],db=config['db'],offline=config['offline'],source=None))
            config['event_db'].parent.mkdir(parents=True,exist_ok=True)
            with closing(sqlite3.connect(config['event_db'])) as state,closing(connect(config['db'])) as sources:
                state.row_factory=sqlite3.Row;ensure_schema(state);config['event_db'].chmod(0o600)
                found=discover(sources,config['aliases'],state,current.date())
                summary['discovery']=found
                if found.get('stale_catalog'):
                    raise CollectionError('mention_catalog_not_fresh')
                summary.update(analyze_pending(config,state,ledger,run,day))
            result=deliver(config)
            summary.update(telegram=result,messaging=result['status'],collection_exit=collected,
                           remaining=ledger.remaining(day,config['max_calls_per_day']))
            bad=bool(collected or summary['analysis_errors'] or result['status'] in {'failed','unknown','error','stale_catalog'})
            summary['status']='error' if bad else ('partial' if summary['pending'] else 'success')
        except (CollectionError,OSError,sqlite3.Error,ValueError,KeyError):
            summary.update(status='error',error_code='mention_workflow_failed')
        summary['requests']=ledger.conn.execute('SELECT count(*) FROM daily_reservations WHERE run_id=?',(run,)).fetchone()[0]
        ledger.finish_run(run,summary['status'],summary);_log(config['log'],summary)
    print(json.dumps(summary,ensure_ascii=False))
    return int(summary['status']=='error')
