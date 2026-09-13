from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from .models import MemoryCandidate, MemoryRecord


class MemoryStore:
    """Append-oriented SQLite memory with FTS5 retrieval."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                run_id TEXT NOT NULL,
                step_id TEXT,
                source TEXT NOT NULL,
                commit_sha TEXT,
                created_at TEXT NOT NULL,
                importance INTEGER NOT NULL,
                immutable INTEGER NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                content, kind, content='memory', content_rowid='id'
            );
            CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
                INSERT INTO memory_fts(rowid, content, kind) VALUES (new.id, new.content, new.kind);
            END;
            """
        )
        self.connection.commit()

    def add(self, record: MemoryRecord) -> MemoryRecord:
        cursor = self.connection.execute(
            """INSERT INTO memory(kind, content, run_id, step_id, source, commit_sha,
               created_at, importance, immutable) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.kind,
                record.content,
                record.run_id,
                record.step_id,
                record.source,
                record.commit_sha,
                record.created_at.isoformat(),
                record.importance,
                int(record.immutable),
            ),
        )
        self.connection.commit()
        return record.model_copy(update={"id": cursor.lastrowid})

    def get(self, record_id: int) -> MemoryRecord:
        row = self.connection.execute("SELECT * FROM memory WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            raise KeyError(record_id)
        return self._to_record(row)

    def update(self, record_id: int, content: str) -> None:
        current = self.get(record_id)
        if current.immutable:
            raise ValueError("immutable memory records cannot be modified")
        self.connection.execute("UPDATE memory SET content = ? WHERE id = ?", (content, record_id))
        self.connection.commit()

    def search(self, query: str, limit: int = 10) -> list[MemoryCandidate]:
        tokens = re.findall(r"\w+", query, flags=re.UNICODE)
        if not tokens or limit <= 0:
            return []
        match_query = " OR ".join(f'"{token}"' for token in tokens)
        rows = self.connection.execute(
            """SELECT m.*, bm25(memory_fts) AS score FROM memory_fts
               JOIN memory m ON m.id = memory_fts.rowid WHERE memory_fts MATCH ?
               ORDER BY score LIMIT ?""",
            (match_query, limit),
        ).fetchall()
        return [
            MemoryCandidate(record=self._to_record(row), score=float(row["score"]))
            for row in rows
        ]

    def recent(self, run_id: str, limit: int = 20) -> list[MemoryRecord]:
        rows = self.connection.execute(
            "SELECT * FROM memory WHERE run_id = ? ORDER BY id DESC LIMIT ?", (run_id, limit)
        ).fetchall()
        return [self._to_record(row) for row in rows]

    def all(self, run_id: str | None = None) -> list[MemoryRecord]:
        if run_id is None:
            rows = self.connection.execute("SELECT * FROM memory ORDER BY id").fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM memory WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [self._to_record(row) for row in rows]

    @staticmethod
    def _to_record(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=row["id"], kind=row["kind"], content=row["content"], run_id=row["run_id"],
            step_id=row["step_id"], source=row["source"], commit_sha=row["commit_sha"],
            created_at=row["created_at"],
            importance=row["importance"],
            immutable=bool(row["immutable"]),
        )

    def close(self) -> None:
        self.connection.close()


class EventLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: object) -> None:
        data = event.model_dump(mode="json") if hasattr(event, "model_dump") else event
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(data, sort_keys=True) + "\n")
