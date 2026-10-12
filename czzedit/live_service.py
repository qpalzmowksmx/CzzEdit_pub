"""One anonymous LIVE job, independently cancellable from VOD/AI work."""
from __future__ import annotations
import json
import queue
import threading
import time
from pathlib import Path

from .common import AppError, Config
from .store import Store, WorkspaceLock
from . import live_store as storage, records
from .live_analyzer import LiveAnalyzer
from .live_transport import PublicChat, inspect_live, now_ms, channel_id

TERMINAL = ('stopped','ended','error','incomplete')


class LiveJobs:
    def __init__(self, root, transport_factory=PublicChat, inspector=inspect_live):
        self.root=Path(root);self.transport_factory=transport_factory;self.inspector=inspector
        self.lock=threading.RLock();self.active=None;self.exports={};self.export_threads=[]
        with Store(self.root): pass
        try:
            with WorkspaceLock(self.root,'.live.lock'), Store(self.root) as store:
                for body in storage.listing(store):
                    if body['state'] not in TERMINAL:
                        body['state']='incomplete';body['error']='앱이 종료되어 수집이 중단되었습니다. 같은 방송이면 이어 기록할 수 있습니다.'
                        body['closed_reason']='process_restart'
                        for marker in storage.candidates(store,body['id']):
                            if marker.get('state') in ('generating','waiting_context'):
                                marker.pop('revision',None);marker['state']='partial';marker['partial_context']=True
                                storage.candidate(store,body['id'],marker)
                        storage.save(store,body)
        except AppError as exc:
            if exc.code!='BUSY': raise

    def inspect(self,url):
        return self.inspector(url)

    def start(self,data,resume=None):
        if data.get('provider','viewer_web')!='viewer_web':
            raise AppError('LIVE_PROVIDER','현재는 공개 무로그인 채팅 연결을 지원합니다.')
        config=Config(sensitive=data.get('sensitive',False),viewer_adaptive=data.get('viewer_adaptive',True))
        channel=channel_id(data.get('url',''))
        with self.lock:
            if self.active and self.active['thread'].is_alive():
                if self.active['channel']==channel and (resume is None or resume==self.active['id']):
                    return {'id':self.active['id'],'reused':True}
                raise AppError('LIVE_BUSY','진행 중인 라이브를 중단한 뒤 다른 방송을 시작하세요.')
            meta=self.inspector(data['url'])
            if meta['status']!='OPEN' or not meta['chat_channel_id'] or meta['live_id'] is None:
                raise AppError('LIVE_OFFLINE','현재 공개 채팅을 받을 수 있는 생방송이 아닙니다.')
            lock=WorkspaceLock(self.root,'.live.lock');lock.__enter__()
            try:
                with Store(self.root) as store:
                    if resume:
                        body=storage.get(store,resume)
                        if body['live_id']!=meta['live_id'] or body['channel_id']!=channel:
                            raise AppError('LIVE_CHANGED','이전 기록과 방송 회차가 다릅니다. 새 수집을 시작하세요.')
                        if body['state'] not in TERMINAL:
                            raise AppError('LIVE_BUSY','이미 수집 중인 기록입니다.')
                        config=Config(**body['config'])
                        body.update(state='starting',error=None,closed_reason=None)
                        body['config']=config.to_dict()
                        storage.save(store,body)
                    else:
                        body=storage.create(store,meta,config)
                cancel=threading.Event(); transport=self.transport_factory()
                job={'id':body['id'],'channel':channel,'cancel':cancel,'transport':transport,'file_lock':lock}
                thread=threading.Thread(target=self._run,args=(job,data['url'],config),name='czz-live-writer',daemon=True)
                job['thread']=thread;self.active=job;thread.start()
                return {'id':body['id'],'reused':False}
            except BaseException:
                lock.__exit__(None,None,None);raise

    def stop(self,sid):
        with self.lock:
            if self.active and self.active['id']==sid and self.active['thread'].is_alive():
                self.active['cancel'].set();self.active['transport'].interrupt()
                return {'id':sid,'state':'stopping'}
        with Store(self.root) as store: return {'id':sid,'state':storage.get(store,sid)['state']}

    def interrupt_connection(self,sid):
        """Local integration-test seam. Not exposed as an HTTP action."""
        with self.lock:
            if self.active and self.active['id']==sid:
                self.active['transport'].interrupt()

    def shutdown(self,timeout=15):
        with self.lock: job=self.active
        if job and job['thread'].is_alive():
            self.stop(job['id']);job['thread'].join(timeout)
            if job['thread'].is_alive():
                with Store(self.root) as store:
                    body=storage.get(store,job['id']);body.update(state='incomplete',error='종료 대기 중 저장이 끝나지 않았습니다. 저장 상태를 확인하세요.')
                    storage.save(store,body)
        deadline=time.monotonic()+max(0,timeout)
        for thread in self.export_threads:
            thread.join(max(0,deadline-time.monotonic()))

    def export(self,sid):
        with Store(self.root) as store: storage.get(store,sid)
        with self.lock:
            old=self.exports.get(sid)
            if old and old['state']=='exporting': return dict(old)
            state={'state':'exporting','id':None};self.exports[sid]=state
            def run():
                try: result=storage.snapshot_export(self.root,sid)
                except Exception:
                    result={'state':'error','error':'TXT를 저장하지 못했습니다. 저장 공간을 확인한 뒤 다시 시도하세요.'}
                with self.lock: self.exports[sid]=result
            thread=threading.Thread(target=run,name='czz-live-export',daemon=True)
            self.export_threads=[t for t in self.export_threads if t.is_alive()]+[thread]
            thread.start()
            return dict(state)

    def status(self,sid):
        with Store(self.root) as store: body=storage.get(store,sid)
        with self.lock: body['export']=dict(self.exports.get(sid,{}))
        return body

    def _run(self,job,url,config):
        sid=job['id'];cancel=job['cancel'];transport=job['transport']
        inbox=queue.Queue(maxsize=10000);finished=threading.Event();failure=[]
        def emit(kind,value):
            try: inbox.put_nowait((kind,value))
            except queue.Full:
                # Let the producer report overload as a failure. Setting cancel
                # here makes the transport treat it as an ordinary user stop.
                raise AppError('LIVE_OVERLOAD','채팅 저장 대기열이 가득 찼습니다. 미저장 구간을 확인하세요.') from None
        def receive():
            try: transport.stream(url,expected,cancel,emit)
            except Exception as exc:
                failure.append(exc if isinstance(exc,AppError) else AppError('LIVE_NETWORK','채팅 수신 중 오류가 발생했습니다. 이어 기록을 시도하세요.'))
            finally: finished.set()
        producer=None
        try:
            with Store(self.root) as store:
                body=storage.get(store,sid,True);expected=body['live_id']
                # A crash can occur after fsync(TXT) but before last_txt_seq is
                # committed. Rebuild once from DB before accepting new events.
                try: storage.sync_txt(store,body,rebuild=True)
                except OSError: body['output_error']='TXT 복구에 실패했습니다. DB는 보존되며 저장 공간을 확인하면 다시 출력할 수 있습니다.'
                storage.save(store,body)
                start=max(0,now_ms()-body['origin_ms'])
                analyzer=LiveAnalyzer(start,config)
                # Candidate IDs remain unique across explicit resumes.
                previous=storage.candidates(store,sid)
                analyzer.groups.serial=max((int(m['id'][1:]) for m in previous),default=0)
                generation_base=body['generation'];segment_start=start;segment_seq=None
                pending=[];connected=False;healthy=None;gap_start=body.get('last_healthy_at_ms')
                gap_start=max(0,gap_start-body['origin_ms']) if gap_start else 0
                monitor=body.get('viewer_monitor_id')
                if config.viewer_adaptive and not monitor:
                    monitor=records.save(store,'viewer_monitor',body['channel_id'],{'label':body['title'],'state':'running','source':'viewer_web','live_session_id':sid,'live_id':expected})
                    body['viewer_monitor_id']=monitor
                if monitor:
                    mon=records.get(store,monitor)['body'];mon['state']='running';records.update(store,monitor,mon)
                def sample(meta,missing=False):
                    if not monitor: return
                    at=meta.get('observed_at',meta.get('at_ms',now_ms()))
                    t=max(0,at-body['origin_ms'])
                    v=meta.get('viewer_count')
                    row={'time_ms':t,'broadcast_ms':t,'received_at':at,'observed_at':at,'live_id':expected,
                         'segment':body['generation'],'viewer_count':None if missing else v,
                         'quality':'ok' if not missing and type(v) is int and v>0 else 'missing','source':'viewer_web'}
                    with store.db: store.db.execute('INSERT INTO viewer_samples(monitor_id,body) VALUES(?,?)',(monitor,json.dumps(row)))
                    analyzer.sample(row)
                    body['viewer_count']=v if not missing else None
                def publish(final=False):
                    nonlocal pending
                    if pending:
                        saved=storage.append(store,body,pending);pending=[];analyzer.add(saved)
                    if connected and healthy is not None:
                        target=max(analyzer.watermark,healthy-body['origin_ms']-5000)
                    else: target=analyzer.watermark
                    if final:
                        target=max(target,(healthy or body['origin_ms'])-body['origin_ms'])
                    with store.db:
                        for mark in analyzer.advance(target,final=final):
                            mark['state']='partial' if mark['partial_context'] else 'confirmed'
                            rev=storage.candidate(store,sid,mark,commit=False)
                            if rev: body['revision']=rev
                        for preview in analyzer.previews():
                            rev=storage.candidate(store,sid,preview,commit=False)
                            if rev: body['revision']=rev
                        body['watermark_ms']=analyzer.watermark
                        if analyzer.last_window:
                            body['last_window']=analyzer.last_window
                            body['viewer_status']=analyzer.last_window.get('viewer_baseline',{}).get('status','missing')
                        body['marker_count']=store.db.execute('SELECT count(*) FROM live_candidates WHERE session_id=?',(sid,)).fetchone()[0]
                        body['analysis_max_seq']=body['last_seq']
                        store.db.execute('UPDATE live_sessions SET body=? WHERE id=?',
                                         (json.dumps({k:v for k,v in body.items() if not k.startswith('_')}),sid))
                    try: storage.sync_txt(store,body,rebuild=bool(body['output_error']))
                    except OSError: body['output_error']='TXT 갱신에 실패했습니다. DB는 보존되며 현재까지 TXT로 다시 저장할 수 있습니다.'
                    storage.save(store,body)
                producer=threading.Thread(target=receive,name='czz-live-socket',daemon=True)
                producer.start();last_publish=time.monotonic();terminal='stopped'
                while not finished.is_set() or not inbox.empty():
                    try: kind,value=inbox.get(timeout=.2)
                    except queue.Empty: kind=None
                    if kind=='chat':
                        raw=dict(value,generation=generation_base+value['generation'])
                        if raw['generation']==body['generation']:
                            pending.append(storage.normalize(body,raw,segment_start,analyzer.watermark))
                            healthy=value['received_at_ms'];body['last_healthy_at_ms']=healthy
                    elif kind=='connected':
                        publish()
                        connected=True;healthy=value['at_ms'];body['last_healthy_at_ms']=healthy
                        segment_start=max(0,healthy-body['origin_ms'])
                        if segment_start>gap_start:
                            body['gaps'].append({'start_ms':gap_start,'end_ms':segment_start,'reason':'unobserved' if not body['generation'] else 'disconnected'})
                            analyzer.gap(gap_start,segment_start)
                        body.update(state='collecting',generation=generation_base+value['generation'],error=None)
                        body['_replay_seen']={}
                        with store.db:
                            cur=store.db.execute('INSERT INTO live_segments(session_id,body) VALUES(?,?)',(sid,json.dumps({'generation':body['generation'],'start_ms':segment_start,'end_ms':None})))
                            segment_seq=cur.lastrowid
                        sample(value['metadata'])
                    elif kind=='heartbeat':
                        if generation_base+value['generation']==body['generation']:
                            healthy=value['at_ms'];body['last_healthy_at_ms']=healthy
                    elif kind=='disconnected':
                        publish();connected=False
                        gap_start=max(0,(healthy or value['at_ms'])-body['origin_ms'])
                        body.update(state='reconnecting',error=value['message'])
                        if segment_seq:
                            with store.db: store.db.execute('UPDATE live_segments SET body=? WHERE seq=?',(json.dumps({'generation':body['generation'],'start_ms':segment_start,'end_ms':gap_start}),segment_seq))
                            segment_seq=None
                    elif kind=='metadata':
                        if value['live_id']==expected: sample(value)
                    elif kind=='viewer_missing': sample(value,True)
                    elif kind=='ended':
                        terminal='ended';body['closed_reason']=value['reason'];cancel.set()
                    if cancel.is_set() and body['state'] not in TERMINAL:
                        body['state']='stopping'
                    if len(pending)>=200 or time.monotonic()-last_publish>=1:
                        publish();last_publish=time.monotonic()
                if failure:
                    terminal='error';body['error']=str(failure[0]);body['error_code']=failure[0].code
                    body['gaps'].append({'start_ms':max(0,(healthy or now_ms())-body['origin_ms']),'end_ms':max(0,now_ms()-body['origin_ms']),'reason':failure[0].code})
                publish(final=True)
                if segment_seq:
                    with store.db: store.db.execute('UPDATE live_segments SET body=? WHERE seq=?',(json.dumps({'generation':body['generation'],'start_ms':segment_start,'end_ms':body['watermark_ms']}),segment_seq))
                body['state']=terminal;body['ended_at_ms']=now_ms()
                if monitor:
                    mon=records.get(store,monitor)['body'];mon['state']='stopped';records.update(store,monitor,mon)
                storage.save(store,body)
                try:
                    result=storage.snapshot_export(self.root,sid);body['checkpoint_id']=result['id']
                    storage.save(store,body)
                    with self.lock: self.exports[sid]=result
                except OSError:
                    body['output_error']='최종 TXT 내보내기에 실패했습니다. DB에서 다시 내보내세요.';storage.save(store,body)
        except Exception as exc:
            cancel.set();transport.interrupt()
            try:
                with Store(self.root) as store:
                    body=storage.get(store,sid);body.update(state='incomplete',error=str(exc) if isinstance(exc,AppError) else '저장 또는 분석을 완료하지 못했습니다. 저장된 원문을 확인하세요.')
                    storage.save(store,body)
            except Exception: pass
        finally:
            cancel.set();transport.interrupt()
            if producer: producer.join(10)
            job['file_lock'].__exit__(None,None,None)
