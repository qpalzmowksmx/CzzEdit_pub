"""Versioned extension records; immutable captures never rewrite rule runs."""
import hashlib
import json
import time
import uuid
from .common import AppError, atomic_text, json_text


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def migrate(db, root):
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version > 3:
        raise AppError('SCHEMA_VERSION','이 저장소는 더 새로운 앱 버전이 필요합니다.')
    if version == 3:
        return
    import sqlite3
    from .store import WorkspaceLock
    from .live_store import SCHEMA
    from contextlib import ExitStack
    with ExitStack() as locks:
        locks.enter_context(WorkspaceLock(root, '.schema.lock'))
        for name in ('.operation.lock', '.live.lock'):
            held = getattr(WorkspaceLock.held, 'paths', {}).get((root/'data'/name).resolve())
            if held is None or held.closed:
                locks.enter_context(WorkspaceLock(root, name))
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version == 3:
            return
        backup = root/'data'/('before-v3.sqlite' if version >= 2 else 'before-v2.sqlite')
        if not backup.exists():
            from contextlib import closing
            with closing(sqlite3.connect(backup)) as target:
                db.backup(target)
        with db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY,kind TEXT NOT NULL,source TEXT NOT NULL,created REAL NOT NULL,body TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS record_source ON records(kind,source,created)')
            db.execute('CREATE TABLE IF NOT EXISTS viewer_samples (seq INTEGER PRIMARY KEY,monitor_id TEXT NOT NULL,body TEXT NOT NULL)')
            for statement in SCHEMA:
                db.execute(statement)
            db.execute('PRAGMA user_version=3')


def save(store, kind, source, body, rid=None):
    rid = rid or uuid.uuid4().hex
    with store.db:
        store.db.execute('INSERT INTO records VALUES(?,?,?,?,?)',
                         (rid, kind, source, time.time(), json_text(body)))
    return rid


def get(store, rid, kind=None):
    row = store.db.execute('SELECT * FROM records WHERE id=?', (rid,)).fetchone()
    if row is None or kind and row['kind'] != kind:
        raise AppError('NOT_FOUND', '저장된 자료를 찾을 수 없습니다.')
    return {**dict(row), 'body': json.loads(row['body'])}


def listing(store, kind, source=None):
    sql, args = 'SELECT * FROM records WHERE kind=?', [kind]
    if source is not None:
        sql += ' AND source=?'; args.append(source)
    return [{**dict(row), 'body': json.loads(row['body'])} for row in store.db.execute(sql+' ORDER BY created DESC,id', args)]


def update(store, rid, body):
    # Used only for job/monitor states. Captures and timeline snapshots are immutable.
    with store.db:
        store.db.execute('UPDATE records SET body=? WHERE id=?', (json_text(body), rid))


def export(store, sid, category, rid, files):
    # sid and rid must be database identities, never client paths.
    store.session(sid)
    get(store, rid)
    for name, value in files.items():
        atomic_text(store.root/'exports'/sid/category/rid/name,
                    value if isinstance(value, str) else json_text(value))
