"""Causal viewer baseline and official, bounded LIVE metadata polling."""
import json
import math
import re
import threading
import time
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from .common import AppError
from . import records


class Baseline:
    def __init__(self, samples):
        self.samples = sorted(samples, key=lambda s:s['time_ms'])
        self.index = 0; self.past = []; self.ref = None; self.epoch = 0
        self.last_change = -math.inf; self.hits = []; self.epochs = []
        self.last_segment = None

    def append_samples(self, rows):
        """Append causal LIVE samples without resetting reference or drop confirmations."""
        last = self.samples[-1]['time_ms'] if self.samples else (self.past[-1]['time_ms'] if self.past else -1)
        fresh = [s for s in rows if s['time_ms'] > last]
        self.samples = self.samples[self.index:] + sorted(fresh, key=lambda s:s['time_ms'])
        self.index = 0

    def average(self, t, minimum):
        start = max(minimum,t-300000)
        if t-start < 300000: return None
        weighted = 0; covered = 0; observations = 0
        for i,s in enumerate(self.past):
            end = min(t, s['time_ms']+120000, self.past[i+1]['time_ms'] if i+1<len(self.past) else t)
            length = max(0,end-max(start,s['time_ms']))
            if length and s.get('quality') == 'ok' and s.get('viewer_count',0)>0:
                covered += length; weighted += length*s['viewer_count']; observations += 1
        return weighted/covered if covered>=240000 and observations>=3 else None

    def at(self, t):
        while self.index<len(self.samples) and self.samples[self.index]['time_ms']<=t:
            s = self.samples[self.index]; self.index += 1
            seg = (s.get('live_id'),s.get('segment',0))
            if self.last_segment is not None and seg!=self.last_segment:
                self.past=[]; self.ref=None; self.hits=[]; self.last_change=-math.inf
            self.last_segment=seg
            now=s['time_ms']; origin=now-s.get('broadcast_ms',now)
            self.past.append(s)
            self.past=[p for p in self.past if p['time_ms']>=now-420000]
            avg=self.average(now,origin+1800000)
            valid=s.get('quality')=='ok' and s.get('viewer_count',0)>0
            if not valid or avg is None:
                self.hits=[]; continue
            if self.ref is None:
                self.ref=avg; self.epoch=max(origin+1800000,now-300000); self.last_change=now
                self.epochs.append({'at_ms':now,'start_ms':self.epoch,'previous':None,'reference':avg,
                                    'mean_start_ms':now-300000,'mean_end_ms':now,'decrease_ratio':None,'rules_version':'0.2.0-viewer',
                                    'reason':'initial','anchor_kind':'on_time' if now==origin+2100000 else 'late'})
            elif now-self.last_change>=300000 and avg<=self.ref*.9+1e-9:
                self.hits.append(now)
                if len(self.hits)>=3 and now-self.hits[0]>=120000:
                    old=self.ref; self.ref=avg; self.epoch=now; self.last_change=now; self.hits=[]
                    self.epochs.append({'at_ms':now,'start_ms':now,'previous':old,'reference':avg,'reason':'drop_10_percent','mean_start_ms':now-300000,'mean_end_ms':now,'decrease_ratio':1-avg/old,'rules_version':'0.2.0-viewer'})
            else: self.hits=[]
        last=self.past[-1] if self.past else None
        fresh=last and last.get('quality')=='ok' and last.get('viewer_count',0)>0 and t-last['time_ms']<120000
        if not fresh: self.hits=[]
        avg=self.average(t, (last['time_ms']-last.get('broadcast_ms',last['time_ms'])+1800000)) if last else None
        mode='viewer_adaptive' if fresh and avg is not None and self.ref is not None else 'chat_only'
        return {'mode':mode,'epoch_start_ms':self.epoch if mode=='viewer_adaptive' else 0,
                'reference':self.ref,'average':avg,'status':'active' if mode=='viewer_adaptive' else 'warming_up' if fresh else 'missing'}


def validate_samples(rows):
    if not isinstance(rows,list) or len(rows)>100000:
        raise AppError('INPUT','시청자 표본은 최대 100,000개 목록이어야 합니다.')
    clean=[]; seen=set()
    for row in rows:
        if not isinstance(row,dict): raise AppError('INPUT','표본 형식이 잘못되었습니다.')
        t=row.get('time_ms'); v=row.get('viewer_count'); q=row.get('quality','ok')
        if type(t) is not int or t<0 or t in seen or q not in ('ok','missing') or (q=='ok' and (type(v) is not int or v<0)):
            raise AppError('INPUT','표본 시각은 중복 없는 양수 밀리초, 시청자는 0 이상 정수여야 합니다.')
        seen.add(t); clean.append({'time_ms':t,'broadcast_ms':t,'viewer_count':v if q=='ok' else None,'quality':q})
    return sorted(clean,key=lambda s:s['time_ms'])


def import_samples(store, rows, label='가져온 표본'):
    samples=validate_samples(rows)
    mid=records.save(store,'viewer_monitor','import',{'label':label,'state':'imported','source':'import','started_at':None})
    with store.db:
        store.db.executemany('INSERT INTO viewer_samples(monitor_id,body) VALUES(?,?)',[(mid,json.dumps(s)) for s in samples])
    return mid


def mapping(store, sid, mid, segments, confirmed):
    store.session(sid); records.get(store,mid,'viewer_monitor')
    if confirmed is not True or not isinstance(segments,list) or not 1<=len(segments)<=100:
        raise AppError('INPUT','방송과 영상의 시간 대응을 확인해야 합니다.')
    end=store.session(sid)['duration_ms']; last=-1; clean=[]
    for seg in segments:
        a,b,c=(seg.get(k) for k in ('video_start_ms','video_end_ms','broadcast_start_ms'))
        if any(type(x) is not int for x in (a,b,c)) or not 0<=a<b<=end or c<0 or a<last:
            raise AppError('INPUT','서로 겹치지 않는 시간 대응 구간이 필요합니다.')
        clean.append({'video_start_ms':a,'video_end_ms':b,'broadcast_start_ms':c}); last=b
    return records.save(store,'viewer_mapping',sid,{'monitor_id':mid,'segments':clean,'confirmed':True})


def mapped_samples(store, sid):
    maps=records.listing(store,'viewer_mapping',sid)
    if not maps: return None
    entry=maps[0]; mapping_data=entry['body']; samples=[]; maximum=0
    for row in store.db.execute('SELECT seq,body FROM viewer_samples WHERE monitor_id=? ORDER BY seq',(mapping_data['monitor_id'],)):
        value=json.loads(row['body']); maximum=max(maximum,row['seq'])
        for i,seg in enumerate(mapping_data['segments']):
            t=value['time_ms']-seg['broadcast_start_ms']+seg['video_start_ms']
            if seg['video_start_ms']<=t<seg['video_end_ms']:
                samples.append({**value,'time_ms':t,'broadcast_ms':value['time_ms'],'segment':i})
    for i,seg in enumerate(mapping_data['segments']):
        samples.append({'time_ms':seg['video_end_ms'],'quality':'missing','viewer_count':None,'segment':i})
    samples.sort(key=lambda s:(s['time_ms'],0 if s.get('quality')=='missing' else 1))
    return {'mapping_id':entry['id'],'monitor_id':mapping_data['monitor_id'],'max_seq':maximum,
            'samples':samples,'data_sha256':records.digest(samples)}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise AppError('REDIRECT','인증 요청의 외부 리디렉션을 허용하지 않습니다.')


def live_info(channel, client_id, secret, cancel, request=None):
    if not re.fullmatch(r'[a-fA-F0-9]{32}',channel): raise AppError('INPUT','채널 ID는 32자리여야 합니다.')
    opener=build_opener(NoRedirect()); cursor=None; seen=set(); deadline=time.monotonic()+45
    for page in range(20):
        if cancel.is_set(): raise AppError('CANCELLED','시청자 수집을 중단했습니다.')
        if page and cancel.wait(1): raise AppError('CANCELLED','중단했습니다.')
        remaining=deadline-time.monotonic()
        if remaining<=0: break
        params={'size':20}
        if cursor: params['next']=cursor
        url='https://openapi.chzzk.naver.com/open/v1/lives?'+urlencode(params)
        try:
            if request: data=request(url)
            else:
                with opener.open(Request(url,headers={'Client-Id':client_id,'Client-Secret':secret}),timeout=min(15,remaining)) as response:
                    data=json.loads(response.read(4*1024*1024))
        except HTTPError as error:
            code='VIEWER_AUTH' if error.code in (401,403) else 'VIEWER_RATE_LIMIT' if error.code==429 else 'VIEWER_HTTP'
            failure=AppError(code,'시청자 조회 인증 또는 호출 한도를 확인하세요.')
            from email.utils import parsedate_to_datetime
            retry=error.headers.get('Retry-After','')
            try: failure.retry_after=max(0,int(retry))
            except ValueError:
                try: failure.retry_after=max(0,parsedate_to_datetime(retry).timestamp()-time.time())
                except (ValueError,TypeError): failure.retry_after=60
            raise failure from None
        except (OSError,ValueError,URLError): raise AppError('VIEWER_NETWORK','시청자 조회에 실패했습니다.') from None
        content=data.get('content',{})
        if not isinstance(content.get('data'),list): raise AppError('VIEWER_SCHEMA','시청자 응답 형식이 변경되었습니다.')
        for live in content['data']:
            if live.get('channelId')==channel:
                if type(live.get('concurrentUserCount')) is not int or live['concurrentUserCount']<0:
                    raise AppError('VIEWER_SCHEMA','시청자 수를 확인하지 못했습니다.')
                return {k:live.get(k) for k in ('liveId','channelId','openDate','concurrentUserCount','liveTitle')}
        cursor=content.get('page',{}).get('next')
        if not cursor or cursor in seen: break
        seen.add(cursor)
    raise AppError('VIEWER_MISSING','조회 예산 안에서 채널을 찾지 못했습니다. 0명으로 처리하지 않습니다.')


class MonitorJobs:
    def __init__(self,root):
        self.root=root; self.jobs={}; self.lock=threading.Lock()
        from .store import Store
        with Store(root) as store:
            for row in records.listing(store,'viewer_monitor'):
                if row['body']['state']=='running':
                    records.update(store,row['id'],dict(row['body'],state='stopped',error='앱 재시작으로 중단되었습니다. 새 기록을 시작하세요.'))

    def start(self,data):
        from .store import Store
        channel=data.get('channel_id',''); client=data.get('client_id',''); secret=data.get('client_secret','')
        if not all(isinstance(v,str) and 0<len(v)<=1024 and not any(c in v for c in '\r\n') for v in (channel,client,secret)) or not re.fullmatch(r'[a-fA-F0-9]{32}',channel):
            raise AppError('INPUT','채널 ID, Client ID, Client Secret이 필요합니다.')
        with self.lock:
            if any(not event.is_set() for event in self.jobs.values()): raise AppError('BUSY','기존 시청자 수집을 먼저 중단하세요.')
            store=Store(self.root)
            try: mid=records.save(store,'viewer_monitor',channel,{'label':channel,'state':'running','source':'official_live','started_at':None})
            finally: store.close()
            event=threading.Event(); self.jobs[mid]=event
            threading.Thread(target=self._run,args=(mid,channel,client,secret,event),daemon=True).start()
        return {'id':mid}

    def _run(self,mid,channel,client,secret,event):
        from .store import Store
        live_id=None; started=None; failures=0
        try:
            while not event.is_set():
                received=int(time.time()*1000); value=None; error=None; retry_after=0
                try:
                    value=live_info(channel,client,secret,event)
                    received=int(time.time()*1000)
                    if live_id is not None and value['liveId']!=live_id:
                        error='방송이 변경되어 중단했습니다. 새 수집을 시작하세요.'; event.set()
                    else:
                        live_id=value['liveId']
                        dt=datetime.fromisoformat(value['openDate'])
                        if dt.tzinfo is None:
                            from datetime import timezone,timedelta
                            dt=dt.replace(tzinfo=timezone(timedelta(hours=9)))
                        started=int(dt.timestamp()*1000); failures=0
                except (AppError,ValueError,TypeError,KeyError) as exc:
                    retry_after=getattr(exc,'retry_after',0)
                    failures+=1; error=str(exc) if isinstance(exc,AppError) else '방송 시작 시각을 확인하지 못했습니다.'
                    if isinstance(exc,AppError) and exc.code=='VIEWER_AUTH': event.set()
                store=Store(self.root)
                try:
                    body=records.get(store,mid)['body']; body.update(state='stopped' if event.is_set() else 'running',error=error,started_at=started,live_id=live_id)
                    records.update(store,mid,body)
                    if started is not None:
                        sample={'time_ms':max(0,received-started),'observed_at':received,'received_at':received,'live_id':live_id,
                                'channel_id':channel,'quality':'ok' if value and not error else 'missing',
                                'viewer_count':value['concurrentUserCount'] if value and not error else None,'source':'official_live'}
                        with store.db: store.db.execute('INSERT INTO viewer_samples(monitor_id,body) VALUES(?,?)',(mid,json.dumps(sample)))
                finally: store.close()
                if event.wait(max(retry_after,min(600,60*2**min(failures,3)))): break
        finally:
            event.set(); store=Store(self.root)
            try:
                body=records.get(store,mid)['body']; body['state']='stopped'; records.update(store,mid,body)
            finally: store.close()

    def stop(self,mid):
        with self.lock:
            if mid in self.jobs: self.jobs[mid].set()
        return {'ok':True}
