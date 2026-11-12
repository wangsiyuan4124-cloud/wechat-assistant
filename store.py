# -*- coding: utf-8 -*-
"""消息队列与去重存储（SQLite 单文件，随服务目录存放）"""
import os
import sqlite3
import threading

_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "queue.db")
_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(_DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _lock, _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                msg_id     TEXT PRIMARY KEY,
                grp        TEXT NOT NULL,
                text       TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'pending',  -- pending / sending / done / failed
                error      TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                updated_at TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_msg_status ON messages(status)")


def enqueue(msg_id, grp, text):
    """幂等入库：msg_id 重复直接忽略。返回 True=新入队，False=重复"""
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO messages (msg_id, grp, text) VALUES (?,?,?)",
            (msg_id, grp, text),
        )
        return cur.rowcount > 0


def claim_next():
    """取下一条 pending 消息并标记 sending，返回 Row 或 None"""
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT * FROM messages WHERE status='pending' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if not row:
            return None
        c.execute(
            "UPDATE messages SET status='sending', updated_at=datetime('now','localtime') WHERE msg_id=?",
            (row["msg_id"],),
        )
        return row


def finish(msg_id, ok, error=""):
    with _lock, _conn() as c:
        c.execute(
            "UPDATE messages SET status=?, error=?, updated_at=datetime('now','localtime') WHERE msg_id=?",
            ("done" if ok else "failed", error, msg_id),
        )


def requeue_stuck():
    """服务重启后，把中断在 sending 的消息放回 pending。
    注意：若发送动作已实际完成但状态未落库，重启后可能重发一次，业务侧需能容忍。"""
    with _lock, _conn() as c:
        c.execute("UPDATE messages SET status='pending' WHERE status='sending'")


def get_status(msg_id):
    """查询消息本地状态，不存在返回 None"""
    with _conn() as c:
        row = c.execute("SELECT status FROM messages WHERE msg_id=?", (msg_id,)).fetchone()
        return row["status"] if row else None


def queue_depth():
    with _conn() as c:
        p = c.execute("SELECT COUNT(*) n FROM messages WHERE status='pending'").fetchone()["n"]
        f = c.execute("SELECT COUNT(*) n FROM messages WHERE status='failed'").fetchone()["n"]
        return {"pending": p, "failed": f}
