"""Explicit private Telegram setup, opt-in and manual/daily delivery entry points."""
from contextlib import chdir
import json
from pathlib import Path
import re
import sqlite3
import sys
import tomllib

from . import CollectionError


def load_config(path):
    try:
        path=Path(path).resolve()
        with path.open('rb') as handle:
            raw=tomllib.load(handle)['telegram']
        if type(raw['enabled']) is not bool:
            raise ValueError
        root=(path.parent/raw.get('project_root','..')).resolve()
        state=(root/raw['state_db']).resolve()
        return {'path':path,'enabled':raw['enabled'],'root':root,'state_db':state}
    except (OSError,KeyError,TypeError,ValueError):
        raise CollectionError('telegram_config_invalid') from None


def send_from_daily(daily):
    """Persistent opt-in is required; keys are never read while disabled."""
    try:
        config=load_config(daily['telegram_config'])
        if not config['enabled']:
            return {'status':'disabled','sent':0}
        if daily['backend']!='ollama':
            return {'status':'fixture_blocked','sent':0}
        from .telegram_credentials import read_credentials
        from .telegram import send_daily
        with chdir(config['root']):
            credential=read_credentials()
        return send_daily(daily['db'],daily['aliases'],daily['policy'],config['state_db'],credential,allow_send=True)
    except (CollectionError,OSError,sqlite3.Error,ValueError):
        return {'status':'error','sent':0,'error_code':'telegram_setup_or_delivery_error'}


def command(args):
    try:
        config=load_config(args.config)
        from .telegram_credentials import setup,status,read_credentials
        if args.action=='setup':
            return setup(args)
        if args.action=='status':
            print(json.dumps({'enabled':config['enabled']}))
            return status(args)
        if args.action in {'enable','disable'}:
            if args.action=='enable':
                with chdir(config['root']):
                    read_credentials()
            text=config['path'].read_text(encoding='utf-8')
            updated,n=re.subn(r'(?m)^enabled\s*=\s*(true|false)\s*(?:#.*)?$',
                             'enabled = '+('true' if args.action=='enable' else 'false'),text)
            if n!=1:
                raise CollectionError('telegram_config_invalid')
            config['path'].write_text(updated,encoding='utf-8')
            print(json.dumps({'status':'enabled' if args.action=='enable' else 'disabled','next_daily_sends':args.action=='enable'}))
            return 0
        from .daily import load_config as load_daily_config
        daily=load_daily_config(args.daily_config)
        if args.action=='catalog':
            path=daily.get('event_db')
            if path is None or not path.is_file():
                print('월별 목록 없음. 먼저 daily를 실행하세요.');return 0
            with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as conn:
                from .daily import now
                month=now().strftime('%Y-%m')
                rows=conn.execute('SELECT payload_json FROM mention_catalog WHERE month=?',(month,)).fetchall()
                records=sorted([json.loads(r[0]) for r in rows],key=lambda r:(r['start'],r['stock']))
                for record in records:
                    print(f"{record['stock']} | {record['start']} ~ {record['end']}")
                print(f'{month} 현재 청약 대상 {len(records)}개 · 메시지 전송 없음')
            return 0
        if daily.get('workflow')=='mention_events':
            daily['telegram_config']=args.config
            from .mention_workflow import deliver
            if args.action=='preview':
                result=deliver(daily,dry_run=True)
                for message in result.get('messages',[]):
                    print(message)
                print(json.dumps(result,ensure_ascii=False))
                return 0
            if not args.allow_send:
                raise CollectionError('telegram_requires_allow_send')
            result=deliver(daily)
            print(json.dumps(result,ensure_ascii=False))
            return int(result['status'] in {'error','unknown','failed','pending','stale_catalog'})
        from .telegram import send_daily,render_digest
        if args.action=='preview':
            digest=render_digest(daily['db'],daily['aliases'],daily['policy'])
            for chunk in digest['chunks']:
                print(chunk)
            print('실제 Telegram 전송 없음')
            return 0
        if not args.allow_send:
            raise CollectionError('telegram_requires_allow_send')
        if not config['enabled']:
            raise CollectionError('telegram_disabled_run_enable_first')
        with chdir(config['root']):
            credential=read_credentials()
        result=send_daily(daily['db'],daily['aliases'],daily['policy'],config['state_db'],credential,allow_send=True)
        print(json.dumps(result,ensure_ascii=False))
        return int(result['status'] in {'error','unknown','failed','pending'})
    except (CollectionError,OSError,sqlite3.Error,ValueError):
        print('Telegram 작업 실패: 설정·로컬 연결 상태를 확인하세요. 비밀 값은 출력하지 않습니다.',file=sys.stderr)
        return 1
