"""Immutable, source-labelled timeline views, including comment-only videos."""
import csv
import io
from . import records
from .common import AppError, video_id
from .transport import PublicTransport
from .review import vod_url, bounded_chats
from .playback import public_source


def source(store, url):
    video = video_id(url)
    existing = [s for s in store.sessions() if s['video_id'] == video]
    if existing:
        return existing[0]['id']
    transport = PublicTransport()
    try:
        data = transport.get('/service/v3/videos/'+video)
    finally:
        transport.close()
    duration = data.get('duration')
    if (type(duration) not in (int,float) or not 0 < duration <= 2678400
            or not isinstance(data.get('videoTitle'),str)
            or str(data.get('videoNo',video)) != video):
        raise AppError('METADATA', '영상 정보를 확인하지 못했습니다.')
    sid = store.create(video,data['videoTitle'],round(duration*1000),'anonymous')
    store.update(sid,state='comments_only')
    return sid


def create(store, sid, run_id=None, comment_id=None, ai_id=None):
    session = store.session(sid); items=[]; run=None
    if run_id:
        run=store.run(run_id)
        if run['session_id'] != sid: raise AppError('SOURCE','분석 영상이 다릅니다.')
        for marker in run['result']['markers']:
            items.append({**marker,'source_type':'rule','anchor_ms':marker['clip_start_ms'],
                          'title':' · '.join(marker['reasons']),'origin_id':marker['id'],'source_ref':run_id})
    if comment_id:
        capture=records.get(store,comment_id,'comments')
        if capture['source'] != sid: raise AppError('SOURCE','댓글 영상이 다릅니다.')
        comments={c['comment_id']:c for c in capture['body']['comments']}
        for rec in capture['body']['recommendations']:
            items.append({**rec,'anchor_ms':rec['start_ms'],'clip_start_ms':rec['start_ms'],
                          'clip_end_ms':rec['end_ms'] or min(session['duration_ms'],rec['start_ms']+30000),
                          'peak_ms':rec['start_ms'],'evidence_ids':[], 'origin_id':rec['id'],
                          'title':rec['description'] or '시청자 추천 시각','source_ref':comment_id,
                          'comment_text':comments[rec['comment_id']]['content']})
    if ai_id:
        ai=records.get(store,ai_id,'ai')
        if ai['source'] != sid: raise AppError('SOURCE','AI 영상이 다릅니다.')
        for rec in ai['body'].get('recommendations',[]):
            items.append({**rec,'source_type':'ai','anchor_ms':rec['start_ms'],
                          'clip_start_ms':rec['start_ms'],'clip_end_ms':rec['end_ms'],
                          'peak_ms':rec['start_ms'],'origin_id':rec['id'],'source_ref':ai_id,'review_state':'미검토',
                          'evidence_ids':[e[5:] for e in rec['evidence_refs'] if e.startswith('chat:')]})
    for item in items:
        item['video_id']=session['video_id'];item['time_basis']='video_playback_ms'
        item['id']=item['source_type']+':'+item['origin_id']
        item['video_url']=vod_url(session['video_id'],item['anchor_ms'])
    items.sort(key=lambda x:(x['anchor_ms'],x['source_type'],x['id']))
    cap=run['result'].get('source_snapshot',{}).get('max_seq',0) if run else store.db.execute(
        'SELECT coalesce(max(seq),0) FROM events WHERE session_id=?',(sid,)).fetchone()[0]
    body={'session_id':sid,'video_id':session['video_id'],'title':session['title'],
          'duration_ms':session['duration_ms'],'max_seq':cap,'run_id':run_id,
          'comment_id':comment_id,'ai_id':ai_id,'items':items,'source_state':session['state']}
    rid=records.save(store,'timeline',sid,body)
    labels={'rule':'자동 탐지','viewer_comment':'시청자 추천','ai':'AI 추천'}
    out=io.StringIO(); writer=csv.writer(out); writer.writerow(['time_ms','source','id','title','timeline_id'])
    lines=[]
    for item in items:
        title=item['title']; safe="'"+title if title.startswith(('=','+','-','@','\t','\r')) else title
        writer.writerow([item['anchor_ms'],labels[item['source_type']],item['id'],safe,rid])
        seconds=item['anchor_ms']//1000
        lines.append(f'{seconds//3600:02}:{seconds//60%60:02}:{seconds%60:02} [{labels[item["source_type"]]}] {title} ({item["id"]})')
    records.export(store,sid,'timelines',rid,{'timeline.json':dict(body,id=rid),'timeline.csv':out.getvalue(),
                   'timeline.txt':f'타임라인 {rid}\n규칙 {run_id} / 댓글 {comment_id} / AI {ai_id}\n'+'\n'.join(lines)+'\n'})
    return rid


def latest(store,sid,run_id=None,ai_id=None):
    captures=records.listing(store,'comments',sid)
    return create(store,sid,run_id,captures[0]['id'] if captures else None,ai_id)


def playback(store,tid):
    body=records.get(store,tid,'timeline')['body']
    transport=PublicTransport()
    try: return public_source(transport.get('/service/v3/videos/'+video_id(body['video_id'])))
    finally: transport.close()


def chats(store,tid,time_ms):
    body=records.get(store,tid,'timeline')['body']
    return bounded_chats(store,body['session_id'],body['max_seq'],body['duration_ms'],time_ms)
