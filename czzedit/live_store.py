"""LIVE persistence, separate from VOD pages and immutable VOD runs."""
from __future__ import annotations
import csv
import hashlib
import hmac
import json
import os
import time
import tempfile
from contextlib import contextmanager
import uuid
from pathlib import Path

from .common import AppError, atomic_text, escaped, json_text, stamp
from . import records

SCHEMA = [
    'CREATE TABLE IF NOT EXISTS live_sessions (id TEXT PRIMARY KEY,created REAL NOT NULL,updated REAL NOT NULL,salt TEXT NOT NULL,body TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS live_segments (seq INTEGER PRIMARY KEY,session_id TEXT NOT NULL REFERENCES live_sessions(id),body TEXT NOT NULL)',
    '''CREATE TABLE IF NOT EXISTS live_events (seq INTEGER PRIMARY KEY,session_id TEXT NOT NULL REFERENCES live_sessions(id),
       event_id TEXT NOT NULL UNIQUE,time_ms INTEGER NOT NULL,event_time_ms INTEGER,received_at_ms INTEGER NOT NULL,
       author_key TEXT,content TEXT NOT NULL,generation INTEGER NOT NULL,late INTEGER NOT NULL,time_quality TEXT NOT NULL,
       provider_key TEXT,history_kind TEXT NOT NULL)''',
    'CREATE INDEX IF NOT EXISTS live_time ON live_events(session_id,time_ms,seq)',
    'CREATE INDEX IF NOT EXISTS live_provider ON live_events(session_id,provider_key,generation)',
    'CREATE TABLE IF NOT EXISTS live_candidates (session_id TEXT NOT NULL REFERENCES live_sessions(id),id TEXT NOT NULL,revision INTEGER NOT NULL,body TEXT NOT NULL,PRIMARY KEY(session_id,id))',
    'CREATE TABLE IF NOT EXISTS live_changes (revision INTEGER PRIMARY KEY,session_id TEXT NOT NULL REFERENCES live_sessions(id),candidate_id TEXT NOT NULL,body TEXT NOT NULL)',
]


def create(store, meta, config):
    sid = uuid.uuid4().hex
    started = int(time.time()*1000)
    body = {**meta,'id':sid,'source_kind':'live','source':'viewer_web','state':'starting','started_at_ms':started,
            'time_basis':'broadcast_elapsed' if meta['broadcast_start_ms'] is not None else 'capture_elapsed',
            'origin_ms':meta['broadcast_start_ms'] if meta['broadcast_start_ms'] is not None else started,
            'config':config.to_dict(),'event_count':0,'marker_count':0,'generation':0,'gaps':[],
            'watermark_ms':0,'last_seq':0,'last_txt_seq':0,'late_count':0,'duplicate_count':0,'invalid_time_count':0,
            'error':None,'output_error':None,'viewer_status':'missing','last_received_at_ms':None,
            'last_healthy_at_ms':None,'closed_reason':None,'checkpoint_id':None,'revision':0}
    with store.db:
        store.db.execute('INSERT INTO live_sessions VALUES(?,?,?,?,?)',(sid,time.time(),time.time(),os.urandom(32).hex(),json_text(body)))
    return body


def get(store, sid, private=False):
    row=store.db.execute('SELECT * FROM live_sessions WHERE id=?',(sid,)).fetchone()
    if row is None: raise AppError('NOT_FOUND','라이브 기록을 찾을 수 없습니다.')
    result=json.loads(row['body'])
    if private: result['_salt']=row['salt']
    return result


def save(store, body):
    public={k:v for k,v in body.items() if not k.startswith('_')}
    with store.db:
        store.db.execute('UPDATE live_sessions SET updated=?,body=? WHERE id=?',(time.time(),json_text(public),body['id']))


def listing(store):
    return [json.loads(r[0]) for r in store.db.execute('SELECT body FROM live_sessions ORDER BY created DESC')]


def events(store, sid, after=None, limit=300, around=None, before_seq=None):
    get(store,sid)
    clauses=['session_id=?'];args=[sid]
    if before_seq is not None: clauses.append('seq<=?');args.append(before_seq)
    if around is not None:
        clauses.append('time_ms>=? AND time_ms<=?');args.extend([max(0,around-30000),around+30000])
        order='ABS(time_ms-?),seq';args.append(around)
    elif after is not None:
        clauses.append('seq>?');args.append(after);order='seq'
    else: order='seq DESC'
    rows=[dict(r) for r in store.db.execute('SELECT * FROM live_events WHERE '+' AND '.join(clauses)+' ORDER BY '+order+' LIMIT ?',[*args,limit])]
    if around is not None: rows.sort(key=lambda r:(r['time_ms'],r['seq']))
    elif after is None: rows.reverse()
    # Internal replay fingerprints are not part of the UI contract.
    for row in rows: row.pop('provider_key',None)
    return rows


def candidate(store,sid,marker,commit=True):
    body=json_text(marker)
    old=store.db.execute('SELECT body FROM live_candidates WHERE session_id=? AND id=?',(sid,marker['id'])).fetchone()
    if old and old[0]==body: return None
    from contextlib import nullcontext
    with store.db if commit else nullcontext():
        cur=store.db.execute('INSERT INTO live_changes(session_id,candidate_id,body) VALUES(?,?,?)',(sid,marker['id'],body))
        revision=cur.lastrowid
        store.db.execute('INSERT INTO live_candidates VALUES(?,?,?,?) ON CONFLICT(session_id,id) DO UPDATE SET revision=excluded.revision,body=excluded.body',
                         (sid,marker['id'],revision,body))
    return revision


def candidates(store,sid):
    get(store,sid)
    rows=[{**json.loads(r['body']),'revision':r['revision']} for r in store.db.execute('SELECT * FROM live_candidates WHERE session_id=?',(sid,))]
    return sorted(rows,key=lambda r:(r['clip_start_ms'],r['id']))


def normalize(body, raw, segment_start, watermark):
    received=raw['received_at_ms'];event=raw.get('event_time_ms')
    valid=type(event) is int and body['origin_ms']<=event<=received+2000
    quality='provider' if valid else 'invalid'
    elapsed=max(0,(event if valid else received)-body['origin_ms'])
    author=raw.get('uid')
    if not isinstance(author,str): author=None
    salt=bytes.fromhex(body['_salt'])
    author_key=hmac.new(salt,author.encode(),'sha256').hexdigest()[:24] if author else None
    fingerprint=json.dumps([event,author,raw.get('message_id'),raw['content']],ensure_ascii=False)
    provider_key=hmac.new(salt,fingerprint.encode(),'sha256').hexdigest() if raw.get('message_id') else None
    history=valid and elapsed<segment_start
    return {'event_id':uuid.uuid4().hex,'time_ms':elapsed,'event_time_ms':event if type(event) is int else None,
            'received_at_ms':received,'author_key':author_key,'content':raw['content'],
            'generation':raw['generation'],'late':int(elapsed<watermark or history or not valid),
            'time_quality':quality,'provider_key':provider_key,'history_kind':'before_connection' if history else 'new'}


def append(store,body,rows):
    saved=[]
    with store.db:
        for e in rows:
            # Exact replay across connections only. Repeated text within one connection is retained.
            if e['provider_key'] and e['history_kind']=='before_connection':
                old=store.db.execute('SELECT count(*) FROM live_events WHERE session_id=? AND provider_key=? AND generation<?',
                                     (body['id'],e['provider_key'],e['generation'])).fetchone()[0]
                seen=body.setdefault('_replay_seen',{})
                key=(e['generation'],e['provider_key'])
                occurrence=seen.get(key,0)+1;seen[key]=occurrence
                if occurrence<=old:
                    body['duplicate_count']+=1
                    continue
            fields=['event_id','time_ms','event_time_ms','received_at_ms','author_key','content','generation','late','time_quality','provider_key','history_kind']
            cur=store.db.execute('INSERT INTO live_events(session_id,'+','.join(fields)+') VALUES('+','.join('?' for _ in range(len(fields)+1))+')',
                                 [body['id'],*(e[k] for k in fields)])
            e={**e,'seq':cur.lastrowid};saved.append(e)
        body['event_count']+=len(saved)
        body['late_count']+=sum(e['late'] for e in saved)
        body['invalid_time_count']+=sum(e['time_quality']=='invalid' for e in saved)
        if saved:
            body['last_seq']=saved[-1]['seq'];body['last_received_at_ms']=max(e['received_at_ms'] for e in saved)
        # Same commit for source metadata and its event boundary.
        store.db.execute('UPDATE live_sessions SET updated=?,body=? WHERE id=?',(time.time(),json_text({k:v for k,v in body.items() if not k.startswith('_')}),body['id']))
    return saved


def directory(store,sid):
    get(store,sid)
    return store.root/'exports'/'live'/sid


def line(e):
    return f"[{stamp(e['time_ms'])}] {e['author_key'] or 'unknown'}\t{escaped(e['content'])}\n"


@contextmanager
def atomic_output(path):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.writing-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='') as out:
            yield out
            out.flush();os.fsync(out.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)


def sync_txt(store,body,rebuild=False):
    path=directory(store,body['id'])/'chat.received.txt'
    if rebuild or not path.exists():
        rows=store.db.execute('SELECT * FROM live_events WHERE session_id=? ORDER BY seq',(body['id'],))
        with atomic_output(path) as out:
            out.write(header(body))
            for e in rows: out.write(line(dict(e)))
    elif body['last_txt_seq']<body['last_seq']:
        rows=store.db.execute('SELECT * FROM live_events WHERE session_id=? AND seq>? ORDER BY seq',(body['id'],body['last_txt_seq']))
        with path.open('a',encoding='utf-8',newline='\n') as out:
            for e in rows: out.write(line(dict(e)))
            out.flush();os.fsync(out.fileno())
    body['last_txt_seq']=body['last_seq'];body['output_error']=None
    atomic_text(path.parent/'manifest.json',json_text({k:v for k,v in body.items() if not k.startswith('_')}))


def header(body):
    return '# CzzEdit LIVE · '+('방송 시작 후 경과' if body['time_basis']=='broadcast_elapsed' else '수집 시작 후 경과')+' · '+str(body['live_id'])+'\n'


def snapshot_export(root,sid):
    """Stream one pinned SQLite read snapshot to disk; never hold a write transaction."""
    from .store import Store
    with Store(root) as store:
        get(store,sid)
        rid=records.save(store,'live_checkpoint',sid,{'state':'exporting'})
        path=directory(store,sid)/'checkpoints'/rid
        store.db.execute('BEGIN')
        try:
            body=get(store,sid)
            marks=candidates(store,sid)
            maximum,count=store.db.execute('SELECT coalesce(max(seq),0),count(*) FROM live_events WHERE session_id=?',(sid,)).fetchone()
            samples=[dict(e) for e in store.db.execute('SELECT seq,body FROM viewer_samples WHERE monitor_id=? ORDER BY seq',(body.get('viewer_monitor_id',''),))]
            fingerprint=hashlib.sha256()
            with atomic_output(path/'chat.txt') as txt, atomic_output(path/'chat.jsonl') as raw:
                txt.write(header(body))
                for row in store.db.execute('SELECT * FROM live_events WHERE session_id=? AND seq<=? ORDER BY time_ms,seq',(sid,maximum)):
                    e=dict(row);txt.write(line(e))
                    public={k:v for k,v in e.items() if k not in ('provider_key','session_id')}
                    encoded=json.dumps(public,ensure_ascii=False,sort_keys=True)+'\n'
                    raw.write(encoded);fingerprint.update(encoded.encode())
            with atomic_output(path/'timeline.txt') as txt, atomic_output(path/'timeline.csv') as csv_file:
                txt.write('# LIVE 편집 후보 · '+body['time_basis']+'\n')
                writer=csv.writer(csv_file);writer.writerow(['start','end','reasons','state'])
                for m in marks:
                    state=m.get('state','confirmed')
                    txt.write(f"{stamp(m['clip_start_ms'])} ~ {stamp(m['clip_end_ms'])} | {', '.join(m['reasons'])} | {state}\n")
                    writer.writerow([stamp(m['clip_start_ms']),stamp(m['clip_end_ms']),', '.join(m['reasons']),state])
                    with atomic_output(path/'markers'/f"{m['id']}.txt") as out:
                        out.write(header(body))
                        for e in store.db.execute('SELECT * FROM live_events WHERE session_id=? AND seq<=? AND time_ms>=? AND time_ms<=? ORDER BY time_ms,seq',
                                                  (sid,maximum,m['clip_start_ms'],m['clip_end_ms'])):
                            out.write(line(dict(e)))
            manifest={**body,'checkpoint_id':rid,'source_kind':'live','rules_version':'0.3.0-live',
                      'max_seq':maximum,'event_count':count,
                      'viewer_max_seq':max((e['seq'] for e in samples),default=0),
                      'data_sha256':fingerprint.hexdigest(),'digest_format':'chat.jsonl UTF-8 bytes',
                      'late_policy':'retain_raw_exclude_finalized_windows',
                      'viewer_samples':[json.loads(e['body']) for e in samples],'markers':marks}
            store.db.commit()
            atomic_text(path/'timeline.json',json_text(marks))
            atomic_text(path/'analysis-manifest.json',json_text(manifest))
            records.update(store,rid,{'state':'completed','manifest':manifest})
        except BaseException:
            store.db.rollback()
            records.update(store,rid,{'state':'error','error':'파일을 저장하지 못했습니다. 다시 내보내기를 실행하세요.'})
            raise
        return {'id':rid,'state':'completed','prefix':f'live/{sid}/checkpoints/{rid}/','count':count}
