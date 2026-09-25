"""SQLite persistence: per-chat history, backend choice, session epoch, agent tasks."""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_chat ON messages (chat_id, id);
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    backend TEXT,
    epoch INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    backend TEXT NOT NULL,
    ref TEXT NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created REAL NOT NULL,
    resolved REAL
);
CREATE INDEX IF NOT EXISTS tasks_chat ON tasks (chat_id, id);
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    added_by INTEGER,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    user_id INTEGER,
    chat_id INTEGER,
    action TEXT NOT NULL,
    task_id INTEGER,
    detail TEXT NOT NULL DEFAULT ''
);
"""


@dataclass
class Task:
    id: int
    chat_id: int
    backend: str
    ref: str
    summary: str
    status: str
    created: float


@dataclass
class AuditEntry:
    id: int
    ts: float
    user_id: int | None
    chat_id: int | None
    action: str
    task_id: int | None
    detail: str


class Store:
    # ponytail: sync sqlite on the event loop; every query is a few-row indexed lookup.
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def add_message(self, chat_id: int, role: str, content: str) -> None:
        self.db.execute("INSERT INTO messages (chat_id, role, content, created) VALUES (?, ?, ?, ?)",
                        (chat_id, role, content, time.time()))

    def history(self, chat_id: int, limit: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?", (chat_id, limit)
        ).fetchall()
        msgs = [{"role": r, "content": c} for r, c in reversed(rows)]
        while msgs and msgs[0]["role"] != "user":  # APIs want the first turn to be the user's
            msgs.pop(0)
        return msgs

    def count_messages(self, chat_id: int) -> int:
        return self.db.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat_id,)).fetchone()[0]

    def _chat(self, chat_id: int) -> tuple[str | None, int]:
        row = self.db.execute("SELECT backend, epoch FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
        return row if row else (None, 0)

    def backend_for(self, chat_id: int) -> str | None:
        return self._chat(chat_id)[0]

    def set_backend(self, chat_id: int, backend: str) -> None:
        self.db.execute("INSERT INTO chats (chat_id, backend) VALUES (?, ?) "
                        "ON CONFLICT(chat_id) DO UPDATE SET backend = excluded.backend", (chat_id, backend))

    def session_id(self, chat_id: int) -> str:
        """Stable per-conversation id for stateful backends; rotates on /reset."""
        return f"telegram-{chat_id}-{self._chat(chat_id)[1]}"

    def reset(self, chat_id: int) -> None:
        self.db.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        self.db.execute("INSERT INTO chats (chat_id, epoch) VALUES (?, 1) "
                        "ON CONFLICT(chat_id) DO UPDATE SET epoch = epoch + 1", (chat_id,))

    def create_task(self, chat_id: int, backend: str, ref: str, summary: str) -> int:
        cur = self.db.execute("INSERT INTO tasks (chat_id, backend, ref, summary, created) VALUES (?, ?, ?, ?, ?)",
                              (chat_id, backend, ref, summary, time.time()))
        return cur.lastrowid

    def get_task(self, task_id: int) -> Task | None:
        row = self.db.execute("SELECT id, chat_id, backend, ref, summary, status, created FROM tasks WHERE id = ?",
                              (task_id,)).fetchone()
        return Task(*row) if row else None

    def find_pending_task(self, backend: str, ref: str) -> Task | None:
        row = self.db.execute("SELECT id, chat_id, backend, ref, summary, status, created FROM tasks "
                              "WHERE backend = ? AND ref = ? AND status = 'pending'", (backend, ref)).fetchone()
        return Task(*row) if row else None

    def resolve_task(self, task_id: int, status: str) -> bool:
        """Move a pending task to a final status. Returns False if it was already resolved."""
        cur = self.db.execute("UPDATE tasks SET status = ?, resolved = ? WHERE id = ? AND status = 'pending'",
                              (status, time.time(), task_id))
        return cur.rowcount == 1

    def set_task_status(self, task_id: int, status: str) -> None:
        self.db.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))

    def list_tasks(self, chat_id: int, limit: int = 10) -> list[Task]:
        rows = self.db.execute("SELECT id, chat_id, backend, ref, summary, status, created FROM tasks "
                               "WHERE chat_id = ? ORDER BY id DESC LIMIT ?", (chat_id, limit)).fetchall()
        return [Task(*r) for r in rows]

    # ---- runtime-managed users (env roles take precedence) -----------------

    def user_role(self, user_id: int) -> str | None:
        row = self.db.execute("SELECT role FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row[0] if row else None

    def set_user(self, user_id: int, role: str, added_by: int) -> None:
        self.db.execute("INSERT INTO users (user_id, role, added_by, created) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(user_id) DO UPDATE SET role = excluded.role, added_by = excluded.added_by",
                        (user_id, role, added_by, time.time()))

    def remove_user(self, user_id: int) -> bool:
        return self.db.execute("DELETE FROM users WHERE user_id = ?", (user_id,)).rowcount == 1

    def list_users(self) -> list[tuple[int, str, int | None]]:
        return self.db.execute("SELECT user_id, role, added_by FROM users ORDER BY role, user_id").fetchall()

    # ---- append-only audit log ------------------------------------------------

    def audit(self, action: str, user_id: int | None = None, chat_id: int | None = None,
              task_id: int | None = None, detail: str = "") -> None:
        self.db.execute("INSERT INTO audit (ts, user_id, chat_id, action, task_id, detail) VALUES (?, ?, ?, ?, ?, ?)",
                        (time.time(), user_id, chat_id, action, task_id, detail))

    def audit_log(self, limit: int = 20, chat_id: int | None = None) -> list[AuditEntry]:
        where, args = ("WHERE chat_id = ?", (chat_id,)) if chat_id is not None else ("", ())
        rows = self.db.execute(f"SELECT id, ts, user_id, chat_id, action, task_id, detail FROM audit {where} "
                               "ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [AuditEntry(*r) for r in rows]
