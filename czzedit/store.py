from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import time
import threading
import uuid
from collections import Counter
from pathlib import Path

from .common import AppError


class WorkspaceLock:
    held = threading.local()
    def __init__(self, root: Path, name=".operation.lock"):
        self.path = root / "data" / name
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if self.file.seek(0, 2) == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise AppError("BUSY", "이 작업 폴더에서 이미 다른 수집 또는 분석이 실행 중입니다.") from None
        paths = getattr(self.held, "paths", {})
        self.held.paths = {**paths, self.path.resolve(): self.file}
        return self

    def __exit__(self, *args):
        if self.file:
            getattr(self.held, "paths", {}).pop(self.path.resolve(), None)
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()


class Store:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __init__(self, root: Path):
        self.root = root.resolve()
        (self.root / "data").mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "data" / "czzedit.sqlite", timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, video_id TEXT NOT NULL, title TEXT NOT NULL,
            duration_ms INTEGER NOT NULL, reported_duration_ms INTEGER NOT NULL,
            salt TEXT NOT NULL, auth_mode TEXT NOT NULL, state TEXT NOT NULL,
            cursor INTEGER NOT NULL DEFAULT 0, pages INTEGER NOT NULL DEFAULT 0,
            issues TEXT NOT NULL DEFAULT '[]', error TEXT,
            created REAL NOT NULL, updated REAL NOT NULL
          );
          CREATE TABLE IF NOT EXISTS pages (
            id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
            cursor INTEGER NOT NULL, digest TEXT NOT NULL, body TEXT NOT NULL,
            received REAL NOT NULL, UNIQUE(session_id, cursor, digest)
          );
          CREATE TABLE IF NOT EXISTS events (
            seq INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
            event_id TEXT NOT NULL, time_ms INTEGER NOT NULL, author_key TEXT,
            content TEXT NOT NULL, page_id INTEGER NOT NULL REFERENCES pages(id),
            UNIQUE(session_id, event_id)
          );
          CREATE INDEX IF NOT EXISTS event_time ON events(session_id, time_ms, seq);
          CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
            created REAL NOT NULL, config TEXT NOT NULL, result TEXT NOT NULL
          );
        """)

        from .records import migrate
        try:
            migrate(self.db, self.root)
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def create(self, video, title, duration_ms, auth_mode):
        sid = uuid.uuid4().hex
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO sessions(id,video_id,title,duration_ms,reported_duration_ms,salt,auth_mode,state,created,updated)"
                " VALUES(?,?,?,?,?,?,?,'collecting',?,?)",
                (sid, video, title, duration_ms, duration_ms, os.urandom(32).hex(), auth_mode, now, now))
        return sid

    def session(self, sid):
        row = self.db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None:
            raise AppError("NOT_FOUND", "저장된 수집 작업을 찾을 수 없습니다.")
        result = dict(row)
        result["issues"] = json.loads(result["issues"])
        return result

    def update(self, sid, **values):
        allowed = {"state", "cursor", "issues", "error", "duration_ms", "auth_mode"}
        if not values.keys() <= allowed:
            raise ValueError("invalid session fields")
        if "issues" in values:
            values["issues"] = json.dumps(values["issues"], ensure_ascii=False)
        values["updated"] = time.time()
        with self.db:
            self.db.execute("UPDATE sessions SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?",
                            [*values.values(), sid])

    def page(self, sid, cursor, payload, rows, next_cursor, finished=False):
        """Page, maximal per-page multiplicity, and checkpoint commit together."""
        session = self.session(sid)
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(body.encode()).hexdigest()
        new_events = []
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO pages(session_id,cursor,digest,body,received) VALUES(?,?,?,?,?)",
                            (sid, cursor, digest, body, time.time()))
            page_id = self.db.execute("SELECT id FROM pages WHERE session_id=? AND cursor=? AND digest=?",
                                      (sid, cursor, digest)).fetchone()[0]
            # previousVideoChats and videoChats represent disjoint time sides in
            # observed responses. The same timestamp/author/text can legitimately repeat.
            occurrences = Counter()
            for row in rows:
                key = (row["playerMessageTime"], row.get("userIdHash"), row["content"])
                occurrences[key] += 1
                fingerprint = json.dumps([*key, occurrences[key]], ensure_ascii=False)
                eid = hashlib.sha256((session["salt"] + fingerprint).encode()).hexdigest()[:32]
                author = row.get("userIdHash")
                author_key = (hmac.new(bytes.fromhex(session["salt"]), author.encode(), "sha256").hexdigest()[:24]
                              if author else None)
                insert = self.db.execute(
                    "INSERT OR IGNORE INTO events(session_id,event_id,time_ms,author_key,content,page_id)"
                    " VALUES(?,?,?,?,?,?)", (sid, eid, key[0], author_key, key[2], page_id))
                if insert.rowcount:
                    new_events.append({"event_id": eid, "time_ms": key[0], "author_key": author_key,
                                       "content": key[2], "seq": insert.lastrowid})
            max_time = max((r["playerMessageTime"] + 1 for r in rows), default=session["duration_ms"])
            self.db.execute(
                "UPDATE sessions SET cursor=?, pages=pages+1, state=?, duration_ms=max(duration_ms,?),"
                " error=NULL,updated=? WHERE id=?",
                (next_cursor, "finished" if finished else "collecting", max_time, time.time(), sid))
        return new_events

    def quarantine(self, sid, cursor, payload):
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO pages(session_id,cursor,digest,body,received) VALUES(?,?,?,?,?)",
                            (sid, cursor, hashlib.sha256(body.encode()).hexdigest(), body, time.time()))

    def events(self, sid, order="time"):
        sorting = "time_ms,seq" if order == "time" else "seq"
        return [dict(r) for r in self.db.execute(
            f"SELECT event_id,time_ms,author_key,content,seq FROM events WHERE session_id=? ORDER BY {sorting}", (sid,))]

    def sessions(self):
        result = []
        for row in self.db.execute("SELECT id FROM sessions ORDER BY created DESC"):
            session = self.session(row[0])
            session.pop("salt")
            session["count"] = self.db.execute("SELECT count(*) FROM events WHERE session_id=?", (row[0],)).fetchone()[0]
            session["runs"] = [dict(x) for x in self.db.execute(
                "SELECT id,created FROM runs WHERE session_id=? ORDER BY created DESC", (row[0],))]
            from . import records
            session["timelines"] = [{"id":r["id"]} for r in records.listing(self,"timeline",session["id"])]
            result.append(session)
        return result

    def save_run(self, sid, config, result):
        rid = uuid.uuid4().hex
        events = self.events(sid)
        fingerprint = hashlib.sha256()
        for event in events:
            fingerprint.update(json.dumps(event, ensure_ascii=False, sort_keys=True).encode())
        session = self.session(sid)
        result = {**result, "source_snapshot": {
            "max_seq": max((e["seq"] for e in events), default=0),
            "event_count": len(events), "data_sha256": fingerprint.hexdigest(),
            "source_state": session["state"], "issues": session["issues"],
        }}
        with self.db:
            self.db.execute("INSERT INTO runs VALUES(?,?,?,?,?)",
                            (rid, sid, time.time(), json.dumps(config), json.dumps(result, ensure_ascii=False)))
        return rid

    def run(self, rid):
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
        if row is None:
            raise AppError("NOT_FOUND", "분석 결과를 찾을 수 없습니다.")
        return {**dict(row), "config": json.loads(row["config"]), "result": json.loads(row["result"])}
