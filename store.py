"""Persistent queue. Ambiguous sends never retry automatically."""
import sqlite3
from contextlib import contextmanager
import threading
import time
from pathlib import Path
_DB = str(Path(__file__).with_name("queue.db"))
_lock = threading.Lock()
@contextmanager
def _conn():
    c = sqlite3.connect(_DB, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        with c:
            yield c
    finally:
        c.close()
def init_db():
    with _lock, _conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS messages (msg_id TEXT PRIMARY KEY, grp TEXT NOT NULL, text TEXT NOT NULL, status TEXT DEFAULT 'pending', error TEXT, created_at TEXT DEFAULT (datetime('now','localtime')), updated_at TEXT, expires_at REAL)")
        if 'expires_at' not in {r[1] for r in c.execute('PRAGMA table_info(messages)')}:
            c.execute('ALTER TABLE messages ADD COLUMN expires_at REAL')
        c.execute('CREATE INDEX IF NOT EXISTS idx_msg_status ON messages(status)')
def enqueue(msg_id, grp, text, expires_at):
    if not all(isinstance(x, str) and x.strip() for x in (msg_id, grp, text)):
        raise ValueError('msgId/group/text must be nonempty strings')
    with _lock, _conn() as c:
        return c.execute('INSERT OR IGNORE INTO messages(msg_id,grp,text,expires_at) VALUES(?,?,?,?)', (msg_id,grp,text,expires_at)).rowcount > 0
def claim_next():
    with _lock, _conn() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute("SELECT * FROM messages WHERE status='pending' ORDER BY created_at,rowid LIMIT 1").fetchone()
        if row:
            c.execute("UPDATE messages SET status='sending',updated_at=datetime('now','localtime') WHERE msg_id=?",(row['msg_id'],))
        return row
def set_status(msg_id, status, error=''):
    if status not in {'pending','sending','done','failed','uncertain','expired'}:
        raise ValueError(status)
    with _lock, _conn() as c:
        c.execute("UPDATE messages SET status=?,error=?,updated_at=datetime('now','localtime') WHERE msg_id=?",(status,error,msg_id))
def finish(msg_id, ok, error=''):
    set_status(msg_id,'done' if ok else 'failed',error)
def requeue_stuck():
    # Legacy name retained for compatibility. Never requeue ambiguous work.
    with _lock, _conn() as c:
        c.execute("UPDATE messages SET status='uncertain',error='process interrupted; inspect before retry',updated_at=datetime('now','localtime') WHERE status='sending'")
def get_status(msg_id):
    with _conn() as c:
        r=c.execute('SELECT status FROM messages WHERE msg_id=?',(msg_id,)).fetchone()
        return r['status'] if r else None
def queue_depth():
    with _conn() as c:
        return dict(c.execute('SELECT status,COUNT(*) FROM messages GROUP BY status').fetchall())
