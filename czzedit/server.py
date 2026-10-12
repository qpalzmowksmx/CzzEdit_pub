from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .common import AppError, Config, video_id
from .service import capture, demo, reanalyze
from .store import Store, WorkspaceLock
from .review import marker_context, vod_url, playback_chats
from .playback import playback_source
from . import records, timeline, comments, ai, viewers, live_store, history
from .live_service import LiveJobs


class Jobs:
    def __init__(self, root):
        self.root = root
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.login = threading.Event()
        self.state = {"phase": "idle", "busy": False}

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def progress(self, delta):
        with self.lock:
            self.state.update(delta)

    def login_ready(self):
        self.progress({"phase": "login", "message": "열린 브라우저에서 본인 시청 계정으로 로그인한 뒤 ‘로그인 완료’를 눌러 주세요."})
        while not self.login.wait(.25):
            if self.cancel.is_set():
                raise AppError("CANCELLED", "로그인을 취소했습니다.")
        self.progress({"phase": "collecting", "message": "시청 계정으로 접근을 확인합니다."})

    def start(self, command, data):
        if command not in ("collect", "demo", "analyze", "comments"):
            raise AppError("INVALID_COMMAND", "지원하지 않는 작업입니다.")
        config = Config(sensitive=data.get("sensitive", False), viewer_adaptive=data.get("viewer_adaptive",False))
        if command == "collect":
            video_id(data.get("url", ""))
            if type(data.get("login", False)) is not bool:
                raise AppError("INPUT", "로그인 설정이 잘못되었습니다.")
        if command == "analyze" and not isinstance(data.get("session_id"), str):
            raise AppError("INPUT", "수집 작업 ID가 필요합니다.")
        with self.lock:
            if self.state.get("busy"):
                raise AppError("BUSY", "진행 중인 작업을 먼저 마쳐 주세요.")
            self.cancel.clear()
            self.login.clear()
            self.state = {"phase": "starting", "busy": True}

        def run():
            try:
                if command == "comments":
                    with Store(self.root) as store:
                        sid=data.get('session_id') or timeline.source(store,data.get('url',''))
                        transport=comments.ViewerCommentTransport(self.login_ready,cancel=self.cancel) if data.get('login') else comments.CommentTransport(cancel=self.cancel)
                        try: cid=comments.capture(store,sid,transport,text=data.get('text'),progress=self.progress)
                        finally: transport.close()
                        runs=store.db.execute('SELECT id FROM runs WHERE session_id=? ORDER BY created DESC LIMIT 1',(sid,)).fetchone()
                        tid=timeline.create(store,sid,runs[0] if runs else None,cid)
                        result={'phase':'done','session_id':sid,'timeline_id':tid,'comment_id':cid}
                elif command == "demo":
                    result = demo(self.root, config, self.progress, self.cancel)
                elif command == "analyze":
                    result = reanalyze(self.root, data["session_id"], config, self.progress, self.cancel)
                else:
                    result = capture(self.root, data["url"], config, data.get("resume"),
                                     data.get("login", False), self.login_ready,
                                     progress=self.progress, cancel=self.cancel)
                if command in ('collect','analyze','demo'):
                    with Store(self.root) as store:
                        warning=None
                        if command=='collect' and data.get('include_comments',True):
                            transport=comments.CommentTransport(cancel=self.cancel)
                            try: comments.capture(store,result['session_id'],transport,progress=self.progress)
                            except AppError as exc: warning='채팅 저장 완료. 영상 댓글: '+str(exc)
                            finally: transport.close()
                        result['timeline_id']=timeline.latest(store,result['session_id'],result['run_id'])
                        if warning: result['output_error']=warning
                self.progress({**result, "busy": False, "message": "저장과 분석이 끝났습니다."})
            except AppError as error:
                self.progress({"phase": "paused" if error.code == "CANCELLED" else "error",
                               "busy": False, "code": error.code, "message": str(error)})
            except (OSError, sqlite3.Error):
                self.progress({"phase": "error", "busy": False, "code": "STORAGE",
                               "message": "저장 공간·파일 권한을 확인해 주세요. 저장된 작업은 목록에서 다시 열 수 있습니다."})
            except Exception:
                # Never display browser exceptions with potentially sensitive URLs.
                self.progress({"phase": "error", "busy": False, "code": "UNEXPECTED",
                               "message": "작업을 완료하지 못했습니다. 저장된 결과를 확인하고 다시 시도해 주세요."})

        threading.Thread(target=run, daemon=True).start()


def make_server(root: Path, port=8765):
    root = root.resolve()
    owner = WorkspaceLock(root, '.server.lock')
    owner.__enter__()
    try:
        with Store(root): pass
        jobs = Jobs(root)
        ai_jobs = ai.Jobs(root)
        monitor_jobs = viewers.MonitorJobs(root)
        live_jobs = LiveJobs(root)
    except BaseException:
        owner.__exit__(None,None,None)
        raise
    token = secrets.token_hex(32)
    html_path = Path(__file__).parent / "ui.html"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def host_ok(self):
            return self.headers.get("Host") in (
                f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

        def authorized(self):
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://127.0.0.1:{self.server.server_port}",
                                         f"http://localhost:{self.server.server_port}"):
                return False
            provided = self.headers.get("X-CzzEdit-Key", "")
            return self.host_ok() and secrets.compare_digest(provided, token)

        def response_headers(self, status, content_type, length, attachment=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                             "connect-src 'self' https://*.navercdn.com; media-src 'self' blob: https://*.navercdn.com; "
                             "worker-src 'self' blob:; frame-ancestors 'none'")
            if attachment:
                self.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
            self.end_headers()

        def reply(self, status, value, content_type="application/json; charset=utf-8", attachment=None):
            body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.response_headers(status, content_type, len(body), attachment)
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def download(self, path):
            # Pin the open file and initial length: a LIVE TXT may keep growing.
            # Large recordings must not be loaded into RAM for every download.
            with path.open('rb') as source:
                remaining = os.fstat(source.fileno()).st_size
                self.response_headers(200, 'application/octet-stream', remaining, path.name)
                try:
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                except OSError:
                    # Headers are already sent; never append a second response.
                    self.close_connection = True

        def do_GET(self):
            if not self.host_ok():
                self.reply(403, {"error": "로컬 주소로 접속해 주세요."})
                return
            parsed = urlparse(self.path)
            assets = {"/live.js": ("live.js", "text/javascript"), "/v2.js": ("v2.js", "text/javascript"), "/player.js": ("player.js", "text/javascript"),
                      "/vendor/hls.min.js": ("vendor/hls.min.js", "text/javascript")}
            if parsed.path in assets:
                name, mime = assets[parsed.path]
                self.reply(200, (Path(__file__).parent / name).read_bytes(), mime)
                return
            if parsed.path == "/":
                html = html_path.read_text(encoding="utf-8").replace("__TOKEN__", token)
                self.reply(200, html.encode(), "text/html; charset=utf-8")
                return
            if parsed.path.startswith("/files/"):
                supplied = parse_qs(parsed.query).get("key", [""])[0]
                if not secrets.compare_digest(supplied, token):
                    self.reply(403, {"error": "접근 키가 필요합니다."})
                    return
                directory = (root / "exports").resolve()
                path = (directory / unquote(parsed.path.removeprefix("/files/"))).resolve()
                if not path.is_relative_to(directory) or path.suffix not in (".txt", ".json", ".jsonl", ".csv") or not path.is_file():
                    self.reply(404, {"error": "파일을 찾을 수 없습니다."})
                    return
                try:
                    self.download(path)
                except OSError:
                    self.reply(500, {"error": "파일을 읽지 못했습니다."})
                return
            if not self.authorized():
                self.reply(403, {"error": "이 앱 화면에서 다시 시도해 주세요."})
                return
            store = None
            try:
                if parsed.path == '/api/live/sessions':
                    store=Store(root); self.reply(200,history.visible(store,live_store.listing(store),'live'))
                elif parsed.path.startswith('/api/live/sessions/'):
                    parts=parsed.path.strip('/').split('/')
                    sid=parts[3]
                    if len(parts)==4:
                        self.reply(200,live_jobs.status(sid))
                    else:
                        store=Store(root)
                        query=parse_qs(parsed.query)
                        def number(name,default,upper=10**15):
                            try: value=int(query.get(name,[default])[0])
                            except (TypeError,ValueError): raise AppError('INPUT','조회 위치가 잘못되었습니다.')
                            if not 0<=value<=upper: raise AppError('INPUT','조회 범위를 확인하세요.')
                            return value
                        if len(parts)==5 and parts[4]=='events':
                            after=number('after_seq',0) if 'after_seq' in query else None
                            rows=live_store.events(store,sid,after=after,limit=max(1,number('limit',300,500)))
                            self.reply(200,{'events':rows,'next_seq':rows[-1]['seq'] if rows else after or 0})
                        elif len(parts)==5 and parts[4]=='candidates':
                            body=live_store.get(store,sid);after=number('after_revision',0)
                            rows=live_store.candidates(store,sid)
                            self.reply(200,{'markers':[m for m in rows if m['revision']>after],
                                            'revision':max((m['revision'] for m in rows),default=0)})
                        elif len(parts)==5 and parts[4]=='chats':
                            body=live_store.get(store,sid)
                            if query.get('basis',[body['time_basis']])[0]!=body['time_basis']:
                                raise AppError('INPUT','방송/수집 시간 기준이 일치하지 않습니다.')
                            self.reply(200,{'chats':live_store.events(store,sid,around=number('time_ms',0)),
                                            'time_basis':body['time_basis']})
                        else: self.reply(404,{'error':'라이브 경로를 찾을 수 없습니다.'})
                elif parsed.path == "/api/status":
                    self.reply(200, jobs.snapshot())
                elif parsed.path == "/api/sessions":
                    store = Store(root)
                    self.reply(200, history.visible(store,store.sessions(),'vod'))
                elif parsed.path.startswith('/api/timelines/'):
                    store=Store(root); parts=parsed.path.strip('/').split('/')
                    if len(parts)==3: self.reply(200,records.get(store,parts[2],'timeline'))
                    elif len(parts)==4 and parts[3]=='playback': self.reply(200,timeline.playback(store,parts[2]))
                    elif len(parts)==4 and parts[3]=='chats':
                        try: position=int(parse_qs(parsed.query).get('time_ms',['0'])[0])
                        except ValueError: raise AppError('INPUT','재생 시간이 잘못되었습니다.')
                        self.reply(200,timeline.chats(store,parts[2],position))
                    else: self.reply(404,{'error':'찾을 수 없습니다.'})
                elif parsed.path.startswith('/api/ai/runs/'):
                    store=Store(root); self.reply(200,records.get(store,parsed.path.rsplit('/',1)[1],'ai'))
                elif parsed.path=='/api/viewers':
                    store=Store(root); result=records.listing(store,'viewer_monitor')
                    for row in result:
                        row['sample_count']=store.db.execute('SELECT count(*) FROM viewer_samples WHERE monitor_id=?',(row['id'],)).fetchone()[0]
                    self.reply(200,result)
                elif parsed.path.startswith("/api/runs/"):
                    store = Store(root)
                    parts = parsed.path.strip("/").split("/")
                    if len(parts) == 4 and parts[3] == "playback":
                        self.reply(200, playback_source(store, parts[2]))
                    elif len(parts) == 4 and parts[3] == "chats":
                        try:
                            position = int(parse_qs(parsed.query).get("time_ms", ["0"])[0])
                        except ValueError:
                            raise AppError("INPUT", "재생 시간이 잘못되었습니다.") from None
                        self.reply(200, playback_chats(store, parts[2], position))
                    elif len(parts) == 5 and parts[3] == "markers":
                        try:
                            offset = int(parse_qs(parsed.query).get("offset", ["0"])[0])
                        except ValueError:
                            raise AppError("INPUT", "채팅 조회 위치가 잘못되었습니다.") from None
                        self.reply(200, marker_context(store, parts[2], parts[4], offset))
                    elif len(parts) == 3:
                        run = store.run(parts[2])
                        session = store.session(run["session_id"])
                        markers = [{**m, "video_url": vod_url(session["video_id"], m["clip_start_ms"])}
                                   for m in run["result"]["markers"]]
                        self.reply(200, {"id": run["id"], "session_id": run["session_id"],
                                         "config": run["config"], "markers": markers,
                                         "windows": run["result"]["windows"]})
                    else:
                        self.reply(404, {"error": "찾을 수 없습니다."})
                else:
                    self.reply(404, {"error": "찾을 수 없습니다."})
            except AppError as error:
                self.reply(404, {"error": str(error)})
            except (OSError, sqlite3.Error):
                self.reply(500, {"error": "저장소를 읽지 못했습니다."})
            finally:
                if store:
                    store.close()

        def do_POST(self):
            if not self.authorized():
                self.reply(403, {"error": "이 앱 화면에서 다시 시도해 주세요."})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 16384:
                    raise ValueError
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError
            except (ValueError, UnicodeError):
                self.reply(400, {"error": "입력 형식이 잘못되었습니다."})
                return
            command = urlparse(self.path).path.removeprefix("/api/")
            try:
                if command in ('history/hide','history/restore'):
                    if jobs.snapshot().get('busy') or ai_jobs.active:
                        raise AppError('BUSY','진행 중인 수집·분석을 마친 뒤 목록을 정리해 주세요.')
                    with WorkspaceLock(root), WorkspaceLock(root,'.live.lock'), Store(root) as store:
                        result=history.change(store,restore=command.endswith('/restore'))
                    self.reply(200,result);return
                elif command=='live/inspect':
                    self.reply(200,live_jobs.inspect(data.get('url','')));return
                elif command=='live/start':
                    self.reply(202,live_jobs.start(data));return
                elif command.startswith('live/sessions/'):
                    parts=command.split('/')
                    if len(parts)!=4: raise AppError('INPUT','라이브 요청을 확인하세요.')
                    sid=parts[2];operation=parts[3]
                    if operation=='stop': result=live_jobs.stop(sid)
                    elif operation=='export': result=live_jobs.export(sid)
                    elif operation=='resume':
                        with Store(root) as store: body=live_store.get(store,sid)
                        result=live_jobs.start({'url':'https://chzzk.naver.com/live/'+body['channel_id']},resume=sid)
                    else: raise AppError('INPUT','지원하지 않는 라이브 요청입니다.')
                    self.reply(202,result);return
                elif command == 'ai/credentials':
                    self.reply(200,ai_jobs.credentials(data.get('key','')));return
                elif command == 'ai/preview':
                    self.reply(200,ai_jobs.preview(data));return
                elif command == 'ai/runs':
                    self.reply(202,ai_jobs.start(data));return
                elif command.startswith('ai/runs/') and command.endswith('/cancel'):
                    self.reply(200,ai_jobs.cancel(command.split('/')[2]));return
                elif command=='viewers/start':
                    self.reply(202,monitor_jobs.start(data));return
                elif command=='viewers/stop':
                    with Store(root) as store:
                        monitor=records.get(store,data.get('id'),'viewer_monitor')
                        if monitor['body'].get('source')=='viewer_web':
                            raise AppError('VIEWER_OWNER','라이브에 연결된 시청자 기록입니다. 라이브 중단 버튼으로 함께 중단하세요.')
                    self.reply(200,monitor_jobs.stop(data.get('id')));return
                elif command=='viewers/import':
                    with Store(root) as store: rid=viewers.import_samples(store,data.get('samples'))
                    self.reply(200,{'id':rid});return
                elif command=='viewers/map':
                    with Store(root) as store: rid=viewers.mapping(store,data.get('session_id'),data.get('monitor_id'),data.get('segments'),data.get('confirmed'))
                    self.reply(200,{'id':rid});return
                elif command=='timelines':
                    with Store(root) as store:
                        if data.get('run_id'):
                            sid=store.run(data['run_id'])['session_id']
                        else: sid=data.get('session_id')
                        rid=timeline.latest(store,sid,data.get('run_id'),data.get('ai_id'))
                    self.reply(200,{'id':rid});return
                elif command == "cancel":
                    jobs.cancel.set()
                elif command == "login-ready":
                    jobs.login.set()
                else:
                    jobs.start(command, data)
                self.reply(202, {"ok": True})
            except (AppError, TypeError, AttributeError, ValueError) as error:
                self.reply(400, {"error": str(error) if isinstance(error, AppError) else "입력 값을 확인해 주세요."})
            except (OSError, sqlite3.Error):
                self.reply(500, {"error":"저장소에 접근하지 못했습니다."})

    class LocalServer(ThreadingHTTPServer):
        closed = False
        def server_close(self):
            if not self.closed:
                self.closed=True
                jobs.cancel.set()
                live_jobs.shutdown()
                for event in list(ai_jobs.active.values())+list(monitor_jobs.jobs.values()): event.set()
                super().server_close()
                owner.__exit__(None,None,None)
    try:
        server = LocalServer(("127.0.0.1", port), Handler)
    except BaseException:
        if not owner.file.closed: owner.__exit__(None,None,None)
        raise
    server.daemon_threads = True
    server.jobs = jobs
    server.ai_jobs = ai_jobs
    server.monitor_jobs = monitor_jobs
    server.live_jobs = live_jobs
    return server


def serve(root, port=8765, open_browser=True):
    server = make_server(root, port)
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"CzzEdit: {url}\n저장 폴더: {root.resolve()}\n종료: Ctrl+C", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    finally:
        server.jobs.cancel.set()
        for event in list(server.ai_jobs.active.values())+list(server.monitor_jobs.jobs.values()): event.set()
        server.server_close()
