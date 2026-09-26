"""SQLite persistence for conversations, so context survives between runs.

Conversations are *sessions* (a titled chat). A Store is bound to ONE session:
load/append/clear act on that session only, so Assistant needs no changes. The
session-management methods (list/create/rename/delete) work on the whole file.
Databases written before sessions existed are migrated in place: their flat
message list becomes one session called "Earlier conversation".
"""
import json
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path.home() / ".raven" / "history.db"
DEFAULT_TITLE = "New chat"
MIGRATED_TITLE = "Earlier conversation"
MAX_TITLE_CHARS = 40


def make_title(text: str) -> str:
    """A session title from a first user message: first line, tidied, truncated."""
    line = " ".join(text.strip().splitlines()[0].split()) if text.strip() else ""
    if not line:
        return DEFAULT_TITLE
    return line if len(line) <= MAX_TITLE_CHARS else line[: MAX_TITLE_CHARS - 1].rstrip() + "…"


class Store:
    def __init__(self, path: Path = DB_PATH, session_id: int | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        # The WebSocket server (server.py) builds the Store on the event-loop
        # thread but Assistant.ask() runs it from a worker thread
        # (asyncio.to_thread); sqlite3's default same-thread check made every
        # server request fail with "SQLite objects created in a thread can
        # only be used in that same thread". The lock serialises access
        # instead, which is what that check was protecting.
        self._lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self._init_schema()
        if session_id is None:
            session_id = self.latest_session_id()
            if session_id is None:
                session_id = self.create_session()
        elif not self._session_exists(session_id):
            raise ValueError(f"No such session: {session_id}")
        self.session_id = session_id

    # -- schema ---------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._lock:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY, "
                "title TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, message TEXT NOT NULL)"
            )
            columns = [row[1] for row in self.db.execute("PRAGMA table_info(messages)")]
            if "session_id" not in columns:  # database from before sessions existed
                self.db.execute("ALTER TABLE messages ADD COLUMN session_id INTEGER")
            orphans = self.db.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id IS NULL"
            ).fetchone()[0]
            if orphans:
                now = time.time()
                cur = self.db.execute(
                    "INSERT INTO sessions (title, created_at, updated_at) VALUES (?, ?, ?)",
                    (MIGRATED_TITLE, now, now),
                )
                self.db.execute(
                    "UPDATE messages SET session_id = ? WHERE session_id IS NULL", (cur.lastrowid,)
                )
            self.db.commit()

    def _session_exists(self, session_id: int) -> bool:
        with self._lock:
            return self.db.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
            ).fetchone() is not None

    # -- this session's messages (what Assistant uses) --------------------------

    def load(self) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT message FROM messages WHERE session_id = ? ORDER BY id", (self.session_id,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def append(self, messages: list[dict]) -> None:
        with self._lock:
            self.db.executemany(
                "INSERT INTO messages (message, session_id) VALUES (?, ?)",
                [(json.dumps(m), self.session_id) for m in messages],
            )
            self.db.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (time.time(), self.session_id)
            )
            row = self.db.execute(
                "SELECT title FROM sessions WHERE id = ?", (self.session_id,)
            ).fetchone()
            if row and row[0] == DEFAULT_TITLE:
                first_user = next(
                    (m["content"] for m in messages
                     if m.get("role") == "user" and isinstance(m.get("content"), str)),
                    None,
                )
                if first_user:
                    self.db.execute(
                        "UPDATE sessions SET title = ? WHERE id = ?",
                        (make_title(first_user), self.session_id),
                    )
            self.db.commit()

    def clear(self) -> None:
        """Forget this session's messages (the session itself stays, retitled on next message)."""
        with self._lock:
            self.db.execute("DELETE FROM messages WHERE session_id = ?", (self.session_id,))
            self.db.execute(
                "UPDATE sessions SET title = ? WHERE id = ?", (DEFAULT_TITLE, self.session_id)
            )
            self.db.commit()

    # -- sessions (whole file) ---------------------------------------------------

    def latest_session_id(self) -> int | None:
        with self._lock:
            row = self.db.execute(
                "SELECT id FROM sessions ORDER BY updated_at DESC, id DESC LIMIT 1"
            ).fetchone()
        return row[0] if row else None

    def list_sessions(self) -> list[dict]:
        """All sessions, most recently active first."""
        with self._lock:
            rows = self.db.execute(
                "SELECT id, title, updated_at FROM sessions ORDER BY updated_at DESC, id DESC"
            ).fetchall()
        return [{"id": r[0], "title": r[1], "updated_at": r[2]} for r in rows]

    def create_session(self, title: str = DEFAULT_TITLE) -> int:
        now = time.time()
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO sessions (title, created_at, updated_at) VALUES (?, ?, ?)",
                (title, now, now),
            )
            self.db.commit()
        return cur.lastrowid

    def rename_session(self, session_id: int, title: str) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE sessions SET title = ? WHERE id = ?", (title.strip() or DEFAULT_TITLE, session_id)
            )
            self.db.commit()

    def delete_session(self, session_id: int) -> None:
        with self._lock:
            self.db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            self.db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            self.db.commit()

    def message_count(self, session_id: int | None = None) -> int:
        with self._lock:
            return self.db.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?",
                (self.session_id if session_id is None else session_id,),
            ).fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self.db.close()
