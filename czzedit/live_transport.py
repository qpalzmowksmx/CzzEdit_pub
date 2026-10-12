"""Anonymous public-web READ protocol. No account cookies or official OAuth."""
from __future__ import annotations

import json
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, build_opener

from .common import AppError
from .viewers import NoRedirect


def channel_id(value):
    if not isinstance(value, str):
        raise AppError('INPUT', '라이브 URL을 입력해 주세요.')
    try:
        u = urlparse(value.strip())
        match = re.fullmatch(r'/live/([a-fA-F0-9]{32})/?', u.path)
        if u.scheme == 'https' and u.hostname == 'chzzk.naver.com' and u.port in (None,443) and not u.username and not u.password and match:
            return match[1].lower()
    except ValueError:
        pass
    raise AppError('LIVE_URL', 'https://chzzk.naver.com/live/채널ID 형식으로 입력해 주세요. 다시보기는 위 입력칸을 사용하세요.')


def now_ms():
    return int(time.time()*1000)


def public_json(url):
    # Callers construct fixed endpoints; no user-controlled destination or redirects.
    try:
        with build_opener(NoRedirect()).open(Request(url, headers={
                'User-Agent':'Mozilla/5.0', 'Referer':'https://chzzk.naver.com/'}), timeout=8) as response:
            body = response.read(4*1024*1024+1)
            if len(body)>4*1024*1024:
                raise AppError('LIVE_SCHEMA', '라이브 응답 크기가 예상 범위를 넘었습니다.')
            value = json.loads(body)
    except HTTPError as exc:
        code = 'LIVE_ACCESS' if exc.code in (401,403) else 'LIVE_RATE' if exc.code == 429 else 'LIVE_NETWORK'
        error = AppError(code, '공개 라이브에 접근할 수 없습니다. 로그인·시청 제한 또는 연결 상태를 확인하세요.')
        try: error.retry_after = min(600,max(1,int(exc.headers.get('Retry-After','60'))))
        except ValueError: error.retry_after = 60
        raise error from None
    except (OSError, URLError, ValueError):
        raise AppError('LIVE_NETWORK','라이브 서버에 연결하지 못했습니다.') from None
    if not isinstance(value,dict):
        raise AppError('LIVE_SCHEMA','라이브 응답 형식이 변경되었습니다.')
    if value.get('code') in (401,403):
        raise AppError('LIVE_ACCESS','무로그인으로 볼 수 없는 방송입니다. 접근 제한을 확인하세요.')
    return value


def inspect_live(url):
    channel = channel_id(url)
    data = public_json('https://api.chzzk.naver.com/service/v2/channels/'+channel+'/live-detail')
    c = data.get('content')
    if data.get('code') != 200 or not isinstance(c,dict):
        raise AppError('LIVE_MISSING','방송 정보를 찾지 못했습니다. 방송이 종료되었는지 확인하세요.')
    start = None
    try:
        dt = datetime.fromisoformat(c['openDate'])
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone(timedelta(hours=9)))
        start = int(dt.timestamp()*1000)
    except (KeyError,TypeError,ValueError):
        pass
    room = c.get('chatChannelId')
    count = c.get('concurrentUserCount')
    return {'channel_id':channel,'live_id':c.get('liveId'),'title':c.get('liveTitle') or '라이브',
            'status':c.get('status'),'chat_channel_id':room if isinstance(room,str) else None,
            'broadcast_start_ms':start,'viewer_count':count if type(count) is int and count>=0 else None,
            'observed_at':now_ms(),'provider':'viewer_web','login_required':bool(c.get('adult'))}


class PublicChat:
    """One socket at a time. A producer feeds a bounded queue owned by LiveJobs.

    emit may raise on overload. Errors never include raw frames, tokens or URLs.
    interrupt() intentionally breaks only this socket, also used by reconnection tests.
    """
    def __init__(self, inspect=inspect_live, request=public_json):
        self.inspect = inspect
        self.request = request
        self.socket = None
        self.lock = threading.Lock()

    def interrupt(self):
        with self.lock:
            if self.socket:
                self.socket.abort()

    def stream(self, url, expected_live, cancel, emit):
        try:
            import websocket
        except ImportError:
            raise AppError('LIVE_SETUP','라이브 통신 패키지가 없습니다. 설치 스크립트를 다시 실행해 주세요.') from None
        generation, failed_since, failures = 0, None, 0
        closed_count = 0
        while not cancel.is_set():
            ws = None
            try:
                meta = self.inspect(url)
                if meta['live_id'] != expected_live:
                    emit('ended', {'reason':'방송 회차가 바뀌었습니다. 새 수집을 시작하세요.'})
                    return
                if meta['status'] != 'OPEN':
                    closed_count += 1
                    if closed_count >= 2:
                        emit('ended', {'reason':'방송이 종료되었습니다.'})
                        return
                    emit('disconnected', {'at_ms':now_ms(),'code':'LIVE_CLOSED_CHECK','message':'방송 종료 여부를 확인하고 있습니다.'})
                    if cancel.wait(60): return
                    continue
                closed_count = 0
                room = meta['chat_channel_id']
                if not room:
                    raise AppError('LIVE_ACCESS','공개 채팅방이 없습니다.')
                p = self.request('https://comm-api.game.naver.com/nng_main/v1/chats/access-token?'+urlencode({'channelId':room,'chatType':'STREAMING'}))
                token = (p.get('content') or {}).get('accessToken')
                if p.get('code') != 200 or not isinstance(token,str) or not token:
                    raise AppError('LIVE_ACCESS','익명 채팅 열람 권한을 받지 못했습니다.')
                route = self.request('https://routing.chat.naver.com/routing/getRouting?serviceId=game')
                hosts = (route.get('result') or {}).get('sessionServerList')
                if not isinstance(hosts,list) or not hosts or any(not isinstance(h,str) or not re.fullmatch(r'[a-zA-Z0-9-]+\.chat\.naver\.com',h) for h in hosts):
                    raise AppError('LIVE_SCHEMA','채팅 서버 안내 형식이 변경되었습니다.')
                if cancel.is_set(): return
                ws = websocket.create_connection('wss://'+random.choice(hosts)+'/chat',timeout=8,
                                                  origin='https://chzzk.naver.com',enable_multithread=True)
                with self.lock: self.socket = ws
                ws.send(json.dumps({'ver':'3','cmd':100,'svcid':'game','cid':room,'sid':None,'tid':1,
                                    'bdy':{'uid':None,'devType':2001,'accTkn':token,'auth':'READ','libVer':'4.11.0'}}))
                token = None
                raw = ws.recv()
                p = json.loads(raw)
                if p.get('cmd') != 10100 or p.get('retCode') != 0:
                    raise AppError('LIVE_ACCESS','채팅 읽기 연결이 거부되었습니다.')
                generation += 1
                emit('connected',{'at_ms':now_ms(),'generation':generation,'metadata':meta})
                failed_since = None
                failures = 0
                ws.settimeout(1)
                last_frame = last_ping = time.monotonic()
                ping_pending = None
                next_meta = last_frame+60
                clock_delta = time.time()-time.monotonic()
                while not cancel.is_set():
                    mono = time.monotonic()
                    if abs(time.time()-mono-clock_delta)>2:
                        raise AppError('LIVE_CLOCK','컴퓨터 시각이 변경되었습니다. 시간 기준을 확인한 뒤 이어 기록하세요.')
                    if ping_pending is not None and mono-ping_pending>10:
                        raise AppError('LIVE_TIMEOUT','채팅 연결 확인 응답이 없어 재연결합니다.')
                    if mono-last_ping>=10 and ping_pending is None:
                        ws.send(json.dumps({'ver':'3','cmd':0}))
                        last_ping = ping_pending = mono
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        raw = None
                    if raw == '':
                        raise AppError('LIVE_DISCONNECTED','채팅 연결이 끊겼습니다. 다시 연결합니다.')
                    if raw is not None:
                        if len(raw)>4*1024*1024:
                            raise AppError('LIVE_SCHEMA','채팅 패킷 크기가 예상 범위를 넘었습니다.')
                        p = json.loads(raw)
                        last_frame = time.monotonic()
                        if p.get('cmd')==0:
                            ws.send(json.dumps({'ver':'3','cmd':10000}))
                            emit('heartbeat',{'at_ms':now_ms(),'generation':generation})
                        elif p.get('cmd')==10000:
                            ping_pending = None
                            emit('heartbeat',{'at_ms':now_ms(),'generation':generation})
                        elif p.get('cmd')==90102:
                            raise AppError('LIVE_DISCONNECTED','서버가 연결을 종료해 다시 연결합니다.')
                        elif p.get('cmd')==93101:
                            rows = p.get('bdy')
                            if not isinstance(rows,list):
                                raise AppError('LIVE_SCHEMA','채팅 메시지 형식이 변경되었습니다.')
                            for row in rows:
                                if not isinstance(row,dict) or not isinstance(row.get('msg'),str):
                                    raise AppError('LIVE_SCHEMA','채팅 본문 형식이 변경되었습니다.')
                                # Only the minimum fields leave the socket owner. No profiles/tokens.
                                emit('chat',{'generation':generation,'received_at_ms':now_ms(),
                                             'uid':row.get('uid'),'content':row['msg'],'event_time_ms':row.get('msgTime'),
                                             'message_id':row.get('msgTid'),'message_type':row.get('msgTypeCode')})
                    if time.monotonic()>=next_meta:
                        try:
                            current = self.inspect(url)
                            emit('metadata',current)
                            if current['live_id'] != expected_live:
                                emit('ended',{'reason':'방송 회차가 바뀌었습니다. 새 수집을 시작하세요.'})
                                return
                            if current['status'] != 'OPEN': closed_count += 1
                            else: closed_count = 0
                            if closed_count>=2:
                                emit('ended',{'reason':'방송이 종료되었습니다.'})
                                return
                            if current['chat_channel_id'] != room and current['status']=='OPEN':
                                raise AppError('LIVE_ROOM','채팅방이 바뀌어 다시 연결합니다.')
                        except AppError as exc:
                            if exc.code=='LIVE_ROOM': raise
                            emit('viewer_missing',{'at_ms':now_ms(),'message':'시청자 자료를 받지 못했습니다.'})
                        next_meta = time.monotonic()+60
            except (AppError,OSError,ValueError,websocket.WebSocketException) as exc:
                if cancel.is_set(): return
                error = exc if isinstance(exc,AppError) else AppError('LIVE_NETWORK','채팅 연결이 끊겨 다시 연결합니다.')
                if error.code in ('LIVE_ACCESS','LIVE_SCHEMA','LIVE_CLOCK','LIVE_OVERLOAD'):
                    raise error from None
                emit('disconnected',{'at_ms':now_ms(),'code':error.code,'message':str(error)})
                failed_since = failed_since or time.monotonic()
                if time.monotonic()-failed_since>=600:
                    raise AppError('LIVE_TIMEOUT','10분간 연결을 복구하지 못했습니다. 저장한 작업에서 이어 기록하세요.') from None
                delay = max(getattr(error,'retry_after',0),min(60,2**min(failures,6))+random.random()*.3)
                failures += 1
                if cancel.wait(delay): return
            finally:
                with self.lock: self.socket = None
                if ws:
                    try: ws.close(timeout=1)
                    except Exception: pass
