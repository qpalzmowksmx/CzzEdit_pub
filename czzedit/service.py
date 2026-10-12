from __future__ import annotations

from pathlib import Path

from .collector import collect
from .common import AppError, Config
from .detector import analyze
from .exporter import export_chat, export_run, received_snapshot
from .store import Store, WorkspaceLock
from .transport import BrowserTransport, PublicTransport


def analyze_saved(store, sid, config, progress=None, cancel=None):
    session = store.session(sid)
    end = session["duration_ms"] if session["state"] == "finished" else session["cursor"]
    gaps = [(i["start_ms"], i["end_ms"]) for i in session["issues"]
            if "start_ms" in i and "end_ms" in i]
    from .viewers import mapped_samples
    viewers = mapped_samples(store, sid) if config.viewer_adaptive else None
    result = analyze(store.events(sid), end, config, gaps, progress, cancel,
                     viewer_samples=viewers['samples'] if viewers else None)
    if viewers:
        result['viewer_snapshot'] = viewers
    rid = store.save_run(sid, config.to_dict(), result)
    export_chat(store, sid)
    directory = export_run(store, rid)
    if viewers:
        from . import records
        import json
        snapshot=records.save(store,'viewer_snapshot',sid,dict(viewers,run_id=rid,epochs=result['viewer_epochs']))
        records.export(store,sid,'viewers',snapshot,{'samples.jsonl':''.join(json.dumps(s)+'\n' for s in viewers['samples']),
                       'epochs.json':result['viewer_epochs']})
    response = {"phase": "done", "session_id": sid, "run_id": rid,
                "directory": str(directory), "marker_count": len(result["markers"]),
                "source_state": session["state"]}
    if progress:
        progress(response)
    return response


def capture(root: Path, url, config=None, resume=None, login=False, login_ready=None,
            max_pages=0, progress=None, cancel=None):
    config = config or Config()
    with WorkspaceLock(root):
        store = Store(root)
        transport = None
        try:
            if login:
                transport = BrowserTransport(login_ready=login_ready or
                    (lambda: input("브라우저에서 본인 시청 계정으로 로그인한 뒤 Enter를 누르세요: ")),
                    cancel=cancel)
            else:
                transport = PublicTransport(cancel=cancel)
            sid = collect(store, transport, url, resume, max_pages, progress,
                          "viewer_browser" if login else "anonymous")
            return analyze_saved(store, sid, config, progress, cancel)
        finally:
            if transport:
                transport.close()
            store.close()


def reanalyze(root: Path, sid, config=None, progress=None, cancel=None):
    with WorkspaceLock(root):
        store = Store(root)
        try:
            return analyze_saved(store, sid, config or Config(), progress, cancel)
        finally:
            store.close()


def demo_rows():
    rows = []
    for start in range(0, 600000, 10000):
        for i in range(10):
            rows.append({"playerMessageTime": start + i * 800, "userIdHash": f"user-{i}",
                         "content": f"평소 대화 {i}", "profile": None})
    for start, content in ((300000, "ㅋㅋㅋㅋ"), (370000, "다시 보고 싶은 장면"), (450000, "헐")):
        for offset in range(0, 20000, 2000):
            for i in range(18):
                rows.append({"playerMessageTime": start + offset + i * 30,
                             "userIdHash": f"reaction-{i}", "content": content, "profile": None})
    for offset in range(100):
        rows.append({"playerMessageTime": 520000 + offset * 10, "userIdHash": "spammer",
                     "content": "ㅋㅋㅋㅋ", "profile": None})
    return sorted(rows, key=lambda r: r["playerMessageTime"])


def demo(root: Path, config=None, progress=None, cancel=None):
    with WorkspaceLock(root):
        store = Store(root)
        try:
            sid = store.create("demo", "오프라인 데모 · 실제 방송 아님", 600000, "demo")
            rows = demo_rows()
            store.page(sid, 0, {"synthetic": True, "rows": rows}, rows, 600000, finished=True)
            received_snapshot(store, sid)
            return analyze_saved(store, sid, config or Config(), progress, cancel)
        finally:
            store.close()
