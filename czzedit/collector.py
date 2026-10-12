from __future__ import annotations

from .common import AppError, numeric, video_id
from .exporter import append_received, export_chat, received_snapshot


def metadata(transport, video):
    data = transport.get(f"/service/v3/videos/{video}")
    duration = numeric(data.get("duration"), "duration")
    if not 0 < duration <= 31 * 24 * 3600 or not isinstance(data.get("videoTitle"), str):
        raise AppError("SCHEMA_CHANGED", "영상 제목 또는 길이를 확인할 수 없습니다.")
    if data.get("videoNo") is not None and str(data["videoNo"]) != video:
        raise AppError("SOURCE_CHANGED", "요청한 영상과 응답 영상이 다릅니다.")
    if data.get("videoChatEnabled") is False:
        raise AppError("CHAT_UNAVAILABLE", "이 다시보기는 채팅 제공이 꺼져 있습니다.")
    return {"title": data["videoTitle"], "duration_ms": round(duration * 1000)}


def validate_page(data, cursor, reported_duration):
    if (not isinstance(data.get("previousVideoChats"), list)
            or not isinstance(data.get("videoChats"), list) or "nextPlayerMessageTime" not in data):
        raise AppError("SCHEMA_CHANGED", "다시보기 채팅 페이지 형식이 변경되었습니다.")
    previous, following = data["previousVideoChats"], data["videoChats"]
    for side, rows in (("previous", previous), ("following", following)):
        for row in rows:
            if not isinstance(row, dict):
                raise AppError("SCHEMA_CHANGED", "채팅 항목이 객체가 아닙니다.")
            t = row.get("playerMessageTime")
            author = row.get("userIdHash")
            if (type(t) is not int or not 0 <= t < reported_duration + 1000
                    or not isinstance(row.get("content"), str)
                    or (author is not None and not isinstance(author, str))):
                raise AppError("SCHEMA_CHANGED", "채팅 시각·본문·작성자 필드를 해석하지 못했습니다.")
            if side == "previous" and t > cursor or side == "following" and t <= cursor:
                raise AppError("BOUNDARY_CHANGED", "채팅 페이지의 시간 경계가 예상과 다릅니다. 원문을 보존하고 중단했습니다.")
    next_cursor = data["nextPlayerMessageTime"]
    if next_cursor is not None and (type(next_cursor) is not int or next_cursor < cursor):
        raise AppError("SCHEMA_CHANGED", "다음 조회 위치가 올바르지 않습니다.")
    if next_cursor is not None and not following:
        raise AppError("PAGE_STALLED", "빈 채팅 페이지에 다음 위치가 있어 수집을 보류했습니다.")
    if next_cursor is not None and (next_cursor <= cursor or next_cursor != max(r["playerMessageTime"] for r in following)):
        raise AppError("PAGE_STALLED", "다음 채팅 위치가 진행되지 않거나 페이지와 일치하지 않습니다.")
    return previous + following, next_cursor


def collect(store, transport, url, sid=None, max_pages=0, progress=None, auth_mode="anonymous"):
    video = video_id(url)
    info = metadata(transport, video)
    if sid:
        session = store.session(sid)
        if session["video_id"] != video or session["reported_duration_ms"] != info["duration_ms"]:
            raise AppError("SOURCE_CHANGED", "저장 당시와 영상 정보가 다릅니다. 새 작업으로 수집해 주세요.")
        if session["state"] == "finished":
            export_chat(store, sid)
            return sid
        store.update(sid, state="collecting", error=None, auth_mode=auth_mode)
    else:
        sid = store.create(video, info["title"], info["duration_ms"], auth_mode)
    output_error = None
    try:
        received_snapshot(store, sid)
    except OSError:
        output_error = "수신 TXT 갱신 실패. DB 저장은 계속합니다."
    if progress:
        progress({"phase": "collecting", "session_id": sid, "title": info["title"],
                  "duration_ms": info["duration_ms"], "output_error": output_error})
    pages_this_run = 0
    try:
        while True:
            session = store.session(sid)
            cursor = session["cursor"]
            data = transport.get(
                f"/service/v1/videos/{video}/chats?playerMessageTime={cursor}&previousVideoChatSize=50")
            try:
                rows, next_cursor = validate_page(data, cursor, info["duration_ms"])
            except AppError:
                store.quarantine(sid, cursor, data)
                raise
            # Timestamp cursors cannot certify more than one page of simultaneous
            # messages. Retain the data and expose the affected point as a gap.
            following = data["videoChats"]
            if len(following) >= 200 and len({r["playerMessageTime"] for r in following}) == 1:
                t = following[0]["playerMessageTime"]
                issue = {"code": "BOUNDARY_AMBIGUOUS", "start_ms": t, "end_ms": t + 1}
                if issue not in session["issues"]:
                    store.update(sid, issues=session["issues"] + [issue])
            finished = next_cursor is None
            checkpoint = next_cursor if next_cursor is not None else max(
                info["duration_ms"], max((r["playerMessageTime"] + 1 for r in rows), default=0))
            new = store.page(sid, cursor, data, rows, checkpoint, finished)
            try:
                if output_error:
                    received_snapshot(store, sid)
                    output_error = None
                else:
                    append_received(store, sid, new)
            except OSError:
                output_error = "수신 TXT 갱신 실패. DB 원본은 저장되어 있습니다."
            pages_this_run += 1
            if progress:
                progress({"phase": "collecting", "position_ms": checkpoint,
                          "pages": session["pages"] + 1, "output_error": output_error})
            if finished:
                break
            if max_pages and pages_this_run >= max_pages:
                store.update(sid, state="paused")
                break
    except (AppError, KeyboardInterrupt) as error:
        store.update(sid, state="paused", error=error.code if isinstance(error, AppError) else "CANCELLED")
        # Preserve the original network/authentication error if exporting also fails.
        try:
            export_chat(store, sid)
        except OSError:
            pass
        raise
    export_chat(store, sid)
    return sid
