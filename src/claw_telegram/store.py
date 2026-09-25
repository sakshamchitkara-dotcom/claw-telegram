"""SQLite persistence: per-conversation history, backend choice, session epoch, agent tasks.

A conversation is a chat, or one forum topic (thread) inside a chat; thread 0
means "no topic". SCHEMA is the 0.1 baseline plus new tables; MIGRATIONS
upgrade it in order and PRAGMA user_version records how far a database got.
"""

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
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('remind', 'every')),
    spec TEXT NOT NULL,
    text TEXT NOT NULL,
    next_run REAL NOT NULL,
    runs INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS schedules_due ON schedules (next_run);
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

MIGRATIONS = [
    # 1: forum topics get their own history, backend and session
    """
    ALTER TABLE messages ADD COLUMN thread_id INTEGER NOT NULL DEFAULT 0;
    DROP INDEX messages_chat;
    CREATE INDEX messages_chat ON messages (chat_id, thread_id, id);
    ALTER TABLE schedules ADD COLUMN thread_id INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE chats_new (
        chat_id INTEGER NOT NULL,
        thread_id INTEGER NOT NULL DEFAULT 0,
        backend TEXT,
        epoch INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (chat_id, thread_id)
    );
    INSERT INTO chats_new (chat_id, backend, epoch) SELECT chat_id, backend, epoch FROM chats;
    DROP TABLE chats;
    ALTER TABLE chats_new RENAME TO chats;
    """,
]


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


@dataclass
class Schedule:
    id: int
    chat_id: int
    user_id: int
    kind: str  # remind (one-shot text) | every (repeating backend prompt)
    spec: str
    text: str
    next_run: float
    runs: int
    thread_id: int = 0


class Store:
    # ponytail: sync sqlite on the event loop; every query is a few-row indexed lookup.
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        for i, sql in enumerate(MIGRATIONS[version:], version + 1):
            self.db.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {i};\nCOMMIT;")

    def add_message(self, chat_id: int, role: str, content: str, thread: int = 0) -> None:
        self.db.execute("INSERT INTO messages (chat_id, thread_id, role, content, created) VALUES (?, ?, ?, ?, ?)",
                        (chat_id, thread, role, content, time.time()))

    def history(self, chat_id: int, limit: int, thread: int = 0) -> list[dict]:
        rows = self.db.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? AND thread_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, thread, limit)).fetchall()
        msgs = [{"role": r, "content": c} for r, c in reversed(rows)]
        while msgs and msgs[0]["role"] != "user":  # APIs want the first turn to be the user's
            msgs.pop(0)
        return msgs

    def count_messages(self, chat_id: int, thread: int = 0) -> int:
        return self.db.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ? AND thread_id = ?",
                               (chat_id, thread)).fetchone()[0]

    def _chat(self, chat_id: int, thread: int) -> tuple[str | None, int]:
        row = self.db.execute("SELECT backend, epoch FROM chats WHERE chat_id = ? AND thread_id = ?",
                              (chat_id, thread)).fetchone()
        return row if row else (None, 0)

    def backend_for(self, chat_id: int, thread: int = 0) -> str | None:
        return self._chat(chat_id, thread)[0]

    def set_backend(self, chat_id: int, backend: str, thread: int = 0) -> None:
        self.db.execute("INSERT INTO chats (chat_id, thread_id, backend) VALUES (?, ?, ?) "
                        "ON CONFLICT(chat_id, thread_id) DO UPDATE SET backend = excluded.backend",
                        (chat_id, thread, backend))

    def session_id(self, chat_id: int, thread: int = 0) -> str:
        """Stable per-conversation id for stateful backends; rotates on /reset."""
        topic = f"-t{thread}" if thread else ""
        return f"telegram-{chat_id}{topic}-{self._chat(chat_id, thread)[1]}"

    def reset(self, chat_id: int, thread: int = 0) -> None:
        self.db.execute("DELETE FROM messages WHERE chat_id = ? AND thread_id = ?", (chat_id, thread))
        self.db.execute("INSERT INTO chats (chat_id, thread_id, epoch) VALUES (?, ?, 1) "
                        "ON CONFLICT(chat_id, thread_id) DO UPDATE SET epoch = epoch + 1", (chat_id, thread))

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

    # ---- scheduled reminders and prompts -------------------------------------

    _SCHED = "SELECT id, chat_id, user_id, kind, spec, text, next_run, runs, thread_id FROM schedules"

    def add_schedule(self, chat_id: int, user_id: int, kind: str, spec: str, text: str, next_run: float,
                     thread: int = 0) -> int:
        return self.db.execute("INSERT INTO schedules (chat_id, thread_id, user_id, kind, spec, text, next_run, "
                               "created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               (chat_id, thread, user_id, kind, spec, text, next_run, time.time())).lastrowid

    def due_schedules(self, now: float) -> list[Schedule]:
        return [Schedule(*r) for r in self.db.execute(f"{self._SCHED} WHERE next_run <= ? ORDER BY next_run",
                                                      (now,)).fetchall()]

    def next_due(self) -> float | None:
        return self.db.execute("SELECT MIN(next_run) FROM schedules").fetchone()[0]

    def list_schedules(self, chat_id: int) -> list[Schedule]:
        return [Schedule(*r) for r in self.db.execute(f"{self._SCHED} WHERE chat_id = ? ORDER BY next_run",
                                                      (chat_id,)).fetchall()]

    def count_schedules(self, user_id: int) -> int:
        return self.db.execute("SELECT COUNT(*) FROM schedules WHERE user_id = ?", (user_id,)).fetchone()[0]

    def get_schedule(self, sid: int) -> Schedule | None:
        row = self.db.execute(f"{self._SCHED} WHERE id = ?", (sid,)).fetchone()
        return Schedule(*row) if row else None

    def reschedule(self, sid: int, next_run: float, ran: bool = True) -> None:
        self.db.execute("UPDATE schedules SET next_run = ?, runs = runs + ? WHERE id = ?", (next_run, int(ran), sid))

    def delete_schedule(self, sid: int) -> bool:
        return self.db.execute("DELETE FROM schedules WHERE id = ?", (sid,)).rowcount == 1
