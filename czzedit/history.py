"""UI-only history visibility. Never delete captures, analysis, or exports."""
import time


def hidden(store, kind):
    prefix = kind + ':'
    return {r[0][len(prefix):] for r in store.db.execute(
        "SELECT source FROM records WHERE kind='hidden_session' AND source LIKE ?",
        (prefix+'%',))}


def visible(store, rows, kind):
    excluded = hidden(store, kind)
    return [row for row in rows if row['id'] not in excluded]


def change(store, restore=False):
    with store.db:
        if restore:
            count = store.db.execute("DELETE FROM records WHERE kind='hidden_session'").rowcount
        else:
            count = 0
            for kind, table in (('vod', 'sessions'), ('live', 'live_sessions')):
                count += store.db.execute(
                    "INSERT OR IGNORE INTO records(id,kind,source,created,body) "
                    "SELECT ? || id,'hidden_session',? || id,?,'{}' FROM " + table,
                    ('hidden:'+kind+':',kind+':',time.time())).rowcount
    return {'changed':count,'restored':restore,'data_deleted':False}
