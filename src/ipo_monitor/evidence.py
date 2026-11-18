"""Common per-stock/source model input with immutable observation provenance."""
import json
import re
from pathlib import Path
import tomllib

from . import CollectionError
from .extraction import digest, plain_text
from .multisource import text_match

VERSION = 'stock-evidence-v1'


def entity_match(text,terms):
    """Distinguish literal names/particles from embedded substring candidates.

    This is an identity-quality hint, not a semantic participation classifier.
    Keep uncertain matches in the input/provenance rather than deleting them.
    """
    boundary=r'[^A-Za-z0-9가-힣]'
    particles='이라고|이라는|입니다|이라면|에서는|으로|부터|까지|처럼|보다|에서|은|는|이|가|을|를|과|와|도|의|에|로|만'
    return any(re.search(r'(?<![A-Za-z0-9가-힣])'+re.escape(term)+
               r'(?=$|'+boundary+r'|(?:'+particles+r')(?=$|'+boundary+r'))',text,re.I)
               for term in terms)


def stock_groups(path):
    if not path.exists():
        return {}
    try:
        with path.open('rb') as f:
            rows=tomllib.load(f).get('stocks',[])
        if not isinstance(rows,list):
            raise ValueError
        groups={};owners={}
        for row in rows:
            if (not isinstance(row,dict) or not isinstance(row.get('name'),str)
                or not row['name'].strip() or not isinstance(row.get('aliases',[]),list)
                or any(not isinstance(a,str) or not a.strip() for a in row.get('aliases',[]))):
                raise ValueError
            name=row['name'].strip()
            if name in groups:
                raise ValueError
            group=list(dict.fromkeys([name,*[a.strip() for a in row.get('aliases',[])]]))
            for term in group:
                key=term.casefold()
                if key in owners and owners[key]!=name:
                    raise ValueError
                owners[key]=name
            groups[name]=group
        return groups
    except (OSError,ValueError):
        raise CollectionError('종목 별칭 설정 오류: 형식/중복/겹치는 별칭을 확인하세요.') from None


def normalize(row):
    payload=json.loads(row['payload_json'])
    content={'text':plain_text(row['title']), 'segments':[], 'events':[], 'ratings':[], 'table_rows':[]}
    summary=payload.get('summary','')
    if not isinstance(summary,str):
        raise CollectionError('관찰 summary 형식 오류')
    if summary:
        content['text']+='\n'+plain_text(summary)
    if row['type']=='youtube':
        captions=payload.get('captions',{})
        if not isinstance(captions,dict):
            raise CollectionError('관찰 자막 형식 오류')
        content['caption_status']=captions.get('status','missing')
        content['caption_language']=captions.get('language')
        if captions.get('status')=='available':
            segments=captions.get('segments',[])
            if not isinstance(segments,list):
                raise CollectionError('관찰 자막 구간 형식 오류')
            for segment in segments:
                if not isinstance(segment,dict) or any(not isinstance(segment.get(k),str) for k in ('start','end','text')):
                    raise CollectionError('관찰 자막 구간 형식 오류')
                content['segments'].append({k:segment[k] for k in ('start','end','text')})
            text=captions.get('text','')
            if not isinstance(text,str):
                raise CollectionError('관찰 자막 텍스트 형식 오류')
            content['text']+='\n'+text
    if row['type']=='ipo_site':
        for target,key in [('events','events'),('ratings','rating_observations'),('table_rows','table_rows')]:
            values=payload.get(key,[])
            if not isinstance(values,list) or any(not isinstance(v,dict) for v in values):
                raise CollectionError('관찰 사이트 항목 형식 오류')
            content[target]=values
        # Keep only public display text in the natural-language evidence field.
        labels=[v.get('raw_label','') for v in content['events']+content['ratings']]
        fields=[value for r in content['table_rows'] for value in r.get('fields',{}).values()]
        if any(not isinstance(v,str) for v in labels+fields):
            raise CollectionError('관찰 사이트 텍스트 형식 오류')
        content['text']+='\n'+'\n'.join(dict.fromkeys(labels+fields))
    return content


def build_inputs(conn,aliases_path,selected=None):
    groups=stock_groups(aliases_path)
    rows=conn.execute('''SELECT a.*,s.type,s.creator,s.name AS source_name FROM source_snapshots a
      JOIN tracked_sources s ON s.id=a.source_id WHERE a.id=(SELECT MAX(b.id) FROM source_snapshots b
      WHERE b.source_id=a.source_id AND b.item_id=a.item_id) ORDER BY a.source_id,a.item_id''').fetchall()
    # Successful full-page membership distinguishes removed site items from history.
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'source_current_catalog' in tables:
        catalogs={r['source_id']:set(json.loads(r['item_ids_json'])) for r in
                  conn.execute('SELECT * FROM source_current_catalog')}
        rows=[r for r in rows if r['type']!='ipo_site' or r['source_id'] not in catalogs
              or r['item_id'] in catalogs[r['source_id']]]
    # Site display names seed the catalog; explicit aliases map only to one named group.
    owners={term.casefold():name for name,terms in groups.items() for term in terms}
    for r in rows:
        if r['type']=='ipo_site':
            name=json.loads(r['payload_json']).get('stock_name',r['title'])
            if not isinstance(name,str) or not name.strip():
                raise CollectionError('사이트 종목명 형식 오류')
            canonical=owners.get(name.casefold(),name)
            groups.setdefault(canonical,[canonical])
            if name not in groups[canonical]:
                groups[canonical].append(name)
    if selected:
        selected_names=[]
        for requested in selected:
            canonical=next((n for n,terms in groups.items() if requested.casefold() in {t.casefold() for t in terms}),requested)
            groups.setdefault(canonical,[canonical])
            selected_names.append(canonical)
        groups={n:groups[n] for n in sorted(set(selected_names))}
    results=[]
    for row in rows:
        content=normalize(row)
        last=conn.execute('SELECT status,error FROM source_attempts WHERE source_id=? ORDER BY id DESC LIMIT 1',(row['source_id'],)).fetchone()
        for stock,terms in sorted(groups.items()):
            if row['type']=='ipo_site':
                name=json.loads(row['payload_json']).get('stock_name',row['title'])
                matched=name.casefold() in {t.casefold() for t in terms}
            else:
                matched=text_match(content['text'],terms)
            if not matched:
                continue
            analysis_input={'version':VERSION,'stock':stock,'aliases':terms,
                'source_id':row['source_id'],'source_type':row['type'],'creator':row['creator'],
                'url':row['url'],'title':row['title'],'published_raw':row['published_raw'],
                'published_at':row['published_at'],'content':content}
            results.append({'input':analysis_input,'input_hash':digest(analysis_input),
                'snapshot_id':row['id'],'observed_at':row['observed_at'],
                'match_quality': 'provided_name' if row['type']=='ipo_site' else
                    ('literal_entity' if entity_match(content['text'],terms) else 'substring_candidate'),
                'source_name':row['source_name'],
                'source_status':last['status'] if last else 'unknown',
                'source_error':last['error'] if last else None})
    return results


def context_hash(entries):
    """Exclude last-check timestamps, include actual revisions and source issues."""
    return digest([{'snapshot_id':e['snapshot_id'],'input_hash':e['input_hash'],
                    'match_quality':e.get('match_quality'),
                    'source_status':e['source_status'],'source_error':e['source_error']}
                   for e in entries])
