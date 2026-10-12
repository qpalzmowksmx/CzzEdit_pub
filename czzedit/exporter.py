from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

from .common import atomic_text, escaped, json_text, stamp


def chat_line(event):
    return f'[{stamp(event["time_ms"])}][{event["author_key"] or "unknown"}][{event["event_id"]}] {escaped(event["content"])}\n'


def source_dir(store, sid):
    # sid always originates from the database; no caller-supplied path is joined.
    sid = store.session(sid)["id"]
    return store.root / "exports" / sid


def received_snapshot(store, sid):
    directory = source_dir(store, sid)
    atomic_text(directory / "chat.received.txt",
                "# CzzEdit chat v1 | mode=VOD | ordering=received | time_basis=video_position\n"
                + "".join(chat_line(e) for e in store.events(sid, "received")))


def append_received(store, sid, events):
    with (source_dir(store, sid) / "chat.received.txt").open("a", encoding="utf-8", newline="\n") as out:
        for event in events:
            out.write(chat_line(event))
        out.flush()


def export_chat(store, sid):
    session = store.session(sid)
    directory = source_dir(store, sid)
    events = store.events(sid)
    complete = session["state"] == "finished"
    metadata = {k: session[k] for k in
                ("id", "video_id", "title", "duration_ms", "reported_duration_ms",
                 "auth_mode", "state", "cursor", "pages", "issues", "error")}
    metadata.update(schema_version=1, time_basis="video_position", event_count=len(events),
                    available_end_ms=session["duration_ms"] if complete else session["cursor"],
                    completeness="unverified" if complete and not session["issues"] else "partial",
                    retrieval_finished=complete,
                    note="API 조회 종료는 플랫폼의 모든 원문 채팅을 받았다는 보장이 아닙니다.")
    atomic_text(directory / "manifest.json", json_text(metadata))
    atomic_text(directory / "chat.txt",
                f'# CzzEdit chat v1 | mode=VOD | time_basis=video_position | completeness={metadata["completeness"]}\n'
                + "".join(chat_line(e) for e in events))
    atomic_text(directory / "chat.jsonl",
                "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events))
    return directory


def csv_safe(value):
    text = str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def export_run(store, rid):
    run = store.run(rid)
    session = store.session(run["session_id"])
    directory = source_dir(store, session["id"]) / rid
    result = run["result"]
    markers = result["markers"]
    events = store.events(session["id"])
    snapshot = result.get("source_snapshot", {})
    if "max_seq" in snapshot:
        events = [e for e in events if e["seq"] <= snapshot["max_seq"]]
    fingerprint = hashlib.sha256()
    for event in events:
        fingerprint.update(json.dumps(event, ensure_ascii=False, sort_keys=True).encode())
    atomic_text(directory / "analysis-manifest.json", json_text({
        "schema_version": 1, "run_id": rid, "source_session_id": session["id"],
        "config": run["config"], "data_sha256": snapshot.get("data_sha256", fingerprint.hexdigest()),
        "event_count": len(events),
        "rules_version": result["rules_version"], "time_basis": "video_position",
        "duration_ms": result["duration_ms"], "source_state": snapshot.get("source_state", session["state"]),
        "completeness": "partial" if snapshot.get("issues", session["issues"])
                        or snapshot.get("source_state", session["state"]) != "finished" else "unverified",
        "issues": snapshot.get("issues", session["issues"]),
    }))
    atomic_text(directory / "timeline.json", json_text(result))
    header = (f'CzzEdit 편집 후보 | {escaped(session["title"])}\n'
              f'시간 기준: 다시보기 재생 위치 | 후보 {len(markers)}개\n'
              "수집 완전성: 미확인. manifest.json의 범위·누락 표시를 함께 확인하세요.\n\n")
    text = [header]
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["id", "time_basis", "start", "peak", "end", "clip_start", "clip_end",
                     "reasons", "N", "U", "ratio", "baseline_ready", "partial_context",
                     "review_state", "evidence_ids"])
    for marker in markers:
        peak = marker["peak_stats"]
        text.append(f'{marker["id"]} [{stamp(marker["clip_start_ms"])} ~ {stamp(marker["clip_end_ms"])}]\n'
                    f'  이유: {", ".join(marker["reasons"])}\n'
                    f'  반응: {stamp(marker["reaction_start_ms"])} ~ {stamp(marker["reaction_end_ms"])}'
                    f' / 정점: {stamp(marker["peak_ms"])}\n'
                    f'  정점 유효 채팅 {peak["n"]}건 / 작성자 {peak["u"]}명'
                    f' / 기준 학습: {"완료" if marker["baseline_ready"] else "임시 후보"}\n'
                    f'  앞뒤 문맥: {"부족" if marker["partial_context"] else "확보"}'
                    f' / 검토: {marker["review_state"]}\n\n')
        writer.writerow([csv_safe(x) for x in [
            marker["id"], "video_position", stamp(marker["reaction_start_ms"]),
            stamp(marker["peak_ms"]), stamp(marker["reaction_end_ms"]),
            stamp(marker["clip_start_ms"]), stamp(marker["clip_end_ms"]),
            ", ".join(marker["reasons"]), peak["n"], peak["u"], peak["ratio"],
            marker["baseline_ready"], marker["partial_context"], marker["review_state"],
            " ".join(marker["evidence_ids"])]])
        evidence = set(marker["evidence_ids"])
        context = [e for e in events if marker["clip_start_ms"] <= e["time_ms"] < marker["clip_end_ms"]]
        atomic_text(directory / "markers" / (marker["id"] + ".txt"),
                    "# * = 판정 근거에 포함된 채팅. 나머지는 앞뒤 문맥입니다.\n"
                    + "".join(("* " if e["event_id"] in evidence else "  ") + chat_line(e) for e in context))
    atomic_text(directory / "timeline.txt", "".join(text))
    atomic_text(directory / "timeline.csv", "\ufeff" + output.getvalue())
    return directory
