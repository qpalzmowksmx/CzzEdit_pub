"""Read-only candidate review, scoped to the analysis snapshot."""
from .common import AppError, video_id


def vod_url(video, time_ms):
    if video == "demo":
        return None
    return f"https://chzzk.naver.com/video/{video_id(video)}?currentTime={max(0, time_ms) // 1000}"


def marker_context(store, rid, marker_id, offset=0, limit=200):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 500:
        raise AppError("INPUT", "채팅 조회 범위가 올바르지 않습니다.")
    run = store.run(rid)
    marker = next((m for m in run["result"]["markers"] if m["id"] == marker_id), None)
    if marker is None:
        raise AppError("NOT_FOUND", "편집 후보를 찾을 수 없습니다.")
    session = store.session(run["session_id"])
    snapshot = run["result"].get("source_snapshot", {})
    max_seq = snapshot.get("max_seq", 9223372036854775807)
    where = "session_id=? AND time_ms>=? AND time_ms<? AND seq<=?"
    args = (session["id"], marker["clip_start_ms"], marker["clip_end_ms"], max_seq)
    total = store.db.execute("SELECT count(*) FROM events WHERE " + where, args).fetchone()[0]
    rows = store.db.execute(
        "SELECT event_id,time_ms,author_key,content FROM events WHERE " + where
        + " ORDER BY time_ms,seq LIMIT ? OFFSET ?", (*args, limit, offset))
    evidence = set(marker["evidence_ids"])
    chats = [{**dict(row), "is_evidence": row["event_id"] in evidence,
              "video_url": vod_url(session["video_id"], row["time_ms"])} for row in rows]
    return {"marker": marker, "video_url": vod_url(session["video_id"], marker["clip_start_ms"]),
            "peak_url": vod_url(session["video_id"], marker["peak_ms"]),
            "chats": chats, "total": total,
            "next_offset": offset + len(chats) if offset + len(chats) < total else None}


def playback_chats(store, rid, time_ms):
    run = store.run(rid)
    end = run["result"]["duration_ms"]
    cap = run["result"].get("source_snapshot", {}).get("max_seq", 9223372036854775807)
    return bounded_chats(store,run['session_id'],cap,end,time_ms)


def bounded_chats(store,sid,cap,end,time_ms):
    if type(time_ms) is not int or not 0 <= time_ms <= 31 * 24 * 3600 * 1000 + 1000:
        raise AppError("INPUT", "재생 시간이 올바르지 않습니다.")
    if time_ms >= end:
        return {"chats": [], "start_ms": end, "end_ms": end, "time_ms": time_ms,
                "truncated": False, "source_end_ms": end}
    start, stop = max(0, time_ms - 30000), min(end, time_ms + 30000)
    args = (sid, cap)
    # Balanced around the playhead, so a burst cannot hide the current messages.
    columns = "event_id,time_ms,author_key,content"
    before = list(store.db.execute(f"SELECT {columns} FROM events WHERE session_id=? AND seq<=?"
                  " AND time_ms>=? AND time_ms<=? AND time_ms<? ORDER BY time_ms DESC,seq DESC LIMIT 151",
                  (*args, start, time_ms, end)))
    after = list(store.db.execute(f"SELECT {columns} FROM events WHERE session_id=? AND seq<=?"
                 " AND time_ms>? AND time_ms<? ORDER BY time_ms,seq LIMIT 151", (*args, time_ms, stop)))
    return {"chats": [dict(r) for r in reversed(before[:150])] + [dict(r) for r in after[:150]],
            "start_ms": start, "end_ms": stop, "time_ms": time_ms,
            "truncated": len(before) > 150 or len(after) > 150,
            "source_end_ms": end}
