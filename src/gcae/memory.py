from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path

from .models import EvidenceKind, EvidenceRecord, MemoryCandidate, MemoryRecord


class MemoryStore:
    """Append-oriented SQLite memory with FTS5 retrieval."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                run_id TEXT NOT NULL,
                source_repo TEXT NOT NULL DEFAULT '',
                scope TEXT NOT NULL DEFAULT 'run',
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
            CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory BEGIN
                INSERT INTO memory_fts(memory_fts, rowid, content, kind)
                    VALUES ('delete', old.id, old.content, old.kind);
                INSERT INTO memory_fts(rowid, content, kind) VALUES (new.id, new.content, new.kind);
            END;
            CREATE TABLE IF NOT EXISTS evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                trajectory_step_id TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL,
                claim_or_subject TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT '',
                source_reference TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                supports TEXT NOT NULL DEFAULT '[]',
                contradicts TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS evidence_run ON evidence(run_id, id);
            CREATE INDEX IF NOT EXISTS evidence_step ON evidence(trajectory_step_id);
            """
        )
        # older databases predate the source_repo column: knowledge stays, but retrieval is
        # scoped from here on, so the column must exist before any search touches it
        columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(memory)").fetchall()
        }
        if "source_repo" not in columns:
            self.connection.execute(
                "ALTER TABLE memory ADD COLUMN source_repo TEXT NOT NULL DEFAULT ''"
            )
        if "scope" not in columns:
            self.connection.execute(
                "ALTER TABLE memory ADD COLUMN scope TEXT NOT NULL DEFAULT 'run'"
            )
        self.connection.commit()

    def add(self, record: MemoryRecord) -> MemoryRecord:
        with self._lock:
            cursor = self.connection.execute(
                """INSERT INTO memory(kind, content, run_id, source_repo, scope, step_id, source,
                   commit_sha, created_at, importance, immutable)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.kind,
                    record.content,
                    record.run_id,
                    record.source_repo,
                    record.scope,
                    record.step_id,
                    record.source,
                    record.commit_sha,
                    record.created_at.isoformat(),
                    record.importance,
                    int(record.immutable),
                ),
            )
            self.connection.commit()
            row_id = cursor.lastrowid
        return record.model_copy(update={"id": row_id})

    def backfill_source_repos(self, mapping: dict[str, str]) -> int:
        """Attribute legacy memory rows (empty ``source_repo``) to their run's repository.

        ``mapping`` is run_id -> source_repo, typically rebuilt from the run state files.
        Only rows the migration left blank are touched; rows that already carry a
        repository are never rewritten. Returns the number of rows updated."""
        updated = 0
        with self._lock:
            for run_id, source_repo in mapping.items():
                if not source_repo:
                    continue
                cursor = self.connection.execute(
                    "UPDATE memory SET source_repo = ? "
                    "WHERE run_id = ? AND (source_repo = '' OR source_repo IS NULL)",
                    (source_repo, run_id),
                )
                updated += cursor.rowcount if cursor.rowcount > 0 else 0
            self.connection.commit()
        return updated

    def get(self, record_id: int) -> MemoryRecord:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM memory WHERE id = ?", (record_id,)
            ).fetchone()
        if row is None:
            raise KeyError(record_id)
        return self._to_record(row)

    def update(self, record_id: int, content: str) -> None:
        current = self.get(record_id)
        if current.immutable:
            raise ValueError("immutable memory records cannot be modified")
        with self._lock:
            self.connection.execute(
                "UPDATE memory SET content = ? WHERE id = ?", (content, record_id)
            )
            self.connection.commit()

    def search(
        self, query: str, limit: int = 10, source_repo: str | None = None
    ) -> list[MemoryCandidate]:
        """FTS retrieval scoped to one repository.

        The knowledge store is cumulative across runs, but a lesson learned in one project
        must not silently become another project's context.  ``source_repo`` restricts the
        hits; legacy rows (empty ``source_repo``) are excluded from scoped searches rather
        than guessed into a project.
        """
        tokens = re.findall(r"\w+", query, flags=re.UNICODE)
        if not tokens or limit <= 0:
            return []
        match_query = " OR ".join(f'"{token}"' for token in tokens)
        if source_repo:
            where = "WHERE memory_fts MATCH ? AND m.source_repo = ?"
            parameters: tuple[object, ...] = (match_query, source_repo)
        else:
            where = "WHERE memory_fts MATCH ?"
            parameters = (match_query,)
        with self._lock:
            rows = self.connection.execute(
                f"""SELECT m.*, bm25(memory_fts) AS score FROM memory_fts
                   JOIN memory m ON m.id = memory_fts.rowid {where}
                   ORDER BY score LIMIT ?""",
                (*parameters, limit),
            ).fetchall()
        return [
            MemoryCandidate(record=self._to_record(row), score=float(row["score"]))
            for row in rows
        ]

    def recent(self, run_id: str, limit: int = 20) -> list[MemoryRecord]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM memory WHERE run_id = ? ORDER BY id DESC LIMIT ?",
                (run_id, limit),
            ).fetchall()
        return [self._to_record(row) for row in rows]

    def all(self, run_id: str | None = None) -> list[MemoryRecord]:
        with self._lock:
            if run_id is None:
                rows = self.connection.execute("SELECT * FROM memory ORDER BY id").fetchall()
            else:
                rows = self.connection.execute(
                    "SELECT * FROM memory WHERE run_id = ? ORDER BY id", (run_id,)
                ).fetchall()
        return [self._to_record(row) for row in rows]

    def counts(self, run_id: str) -> dict[str, int]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT kind, COUNT(*) AS total FROM memory WHERE run_id = ? GROUP BY kind",
                (run_id,),
            ).fetchall()
        return {str(row["kind"]): int(row["total"]) for row in rows}

    @staticmethod
    def _to_record(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=row["id"], kind=row["kind"], content=row["content"], run_id=row["run_id"],
            source_repo=row["source_repo"] if "source_repo" in row.keys() else "",
            scope=row["scope"] if "scope" in row.keys() and row["scope"] else "run",
            step_id=row["step_id"], source=row["source"], commit_sha=row["commit_sha"],
            created_at=row["created_at"],
            importance=row["importance"],
            immutable=bool(row["immutable"]),
        )

    # ------------------------------------------------------------------ evidence ledger

    def add_evidence(self, record: EvidenceRecord) -> EvidenceRecord:
        """Append one evidence record; returns it with its ledger id."""
        with self._lock:
            cursor = self.connection.execute(
                """INSERT INTO evidence(run_id, trajectory_step_id, kind, claim_or_subject,
                   source_type, source_reference, summary, supports, contradicts, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.run_id,
                    record.trajectory_step_id,
                    str(record.kind),
                    record.claim_or_subject,
                    record.source_type,
                    record.source_reference,
                    record.summary,
                    json.dumps(record.supports),
                    json.dumps(record.contradicts),
                    record.created_at.isoformat(),
                ),
            )
            self.connection.commit()
            row_id = cursor.lastrowid
        return record.model_copy(update={"id": row_id})

    def evidence_for_run(self, run_id: str) -> list[EvidenceRecord]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM evidence WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [self._to_evidence(row) for row in rows]

    def evidence_for_step(self, trajectory_step_id: str) -> list[EvidenceRecord]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM evidence WHERE trajectory_step_id = ? ORDER BY id",
                (trajectory_step_id,),
            ).fetchall()
        return [self._to_evidence(row) for row in rows]

    @staticmethod
    def _to_evidence(row: sqlite3.Row) -> EvidenceRecord:
        try:
            supports = json.loads(row["supports"])
            contradicts = json.loads(row["contradicts"])
        except (json.JSONDecodeError, TypeError):  # pragma: no cover - corrupt ledger row
            supports, contradicts = [], []
        return EvidenceRecord(
            id=row["id"],
            run_id=row["run_id"],
            trajectory_step_id=row["trajectory_step_id"],
            kind=EvidenceKind(row["kind"]),
            claim_or_subject=row["claim_or_subject"],
            source_type=row["source_type"],
            source_reference=row["source_reference"],
            summary=row["summary"],
            supports=[str(item) for item in supports],
            contradicts=[str(item) for item in contradicts],
        )

    def close(self) -> None:
        with self._lock:
            self.connection.close()


class EventLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # the run thread and the provider heartbeat thread both append: keep lines intact
        self._lock = threading.Lock()

    def append(self, event: object) -> None:
        data = event.model_dump(mode="json") if hasattr(event, "model_dump") else event
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(data, sort_keys=True) + "\n")
