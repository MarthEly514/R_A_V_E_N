"""SQLite persistence for conversation history, so context survives between runs."""
import json
import sqlite3
from pathlib import Path

DB_PATH = Path.home() / ".raven" / "history.db"


class Store:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, message TEXT NOT NULL)"
        )
        self.db.commit()

    def load(self) -> list[dict]:
        rows = self.db.execute("SELECT message FROM messages ORDER BY id").fetchall()
        return [json.loads(row[0]) for row in rows]

    def append(self, messages: list[dict]) -> None:
        self.db.executemany(
            "INSERT INTO messages (message) VALUES (?)",
            [(json.dumps(m),) for m in messages],
        )
        self.db.commit()

    def clear(self) -> None:
        self.db.execute("DELETE FROM messages")
        self.db.commit()
