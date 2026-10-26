from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ion.contracts import MemoryRecord


class MemoryStore:
    """Small source-linked repository knowledge database.

    Facts are advisory records. Scope and lifecycle status are applied before
    ranking, so stale or forgotten knowledge cannot silently re-enter prompts.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError("memory database path cannot be a symlink")
        self.connection = sqlite3.connect(self.path)
        os.chmod(self.path, 0o600)
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                memory_id TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                fact_key TEXT NOT NULL,
                text TEXT NOT NULL,
                evidence_kind TEXT NOT NULL,
                status TEXT NOT NULL,
                source_refs_json TEXT NOT NULL,
                supporting_hashes_json TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                valid_from TEXT,
                valid_until TEXT,
                supersedes_id TEXT,
                extraction_version TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS memories_scope_status ON memories(scope, status);
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(memory_id UNINDEXED, scope UNINDEXED, fact_key, text);
            """
        )
        self.connection.commit()

    def observe(self, candidate: MemoryRecord | dict) -> MemoryRecord:
        record = candidate if isinstance(candidate, MemoryRecord) else MemoryRecord.model_validate(candidate)
        with self.connection:
            self.connection.execute(
                "INSERT INTO memories(memory_id, scope, fact_key, text, evidence_kind, status, source_refs_json, supporting_hashes_json, observed_at, valid_from, valid_until, supersedes_id, extraction_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record.memory_id, record.scope, record.fact_key, record.text, record.evidence_kind, record.status,
                 json.dumps(record.source_refs), json.dumps(record.supporting_hashes), record.observed_at.isoformat(),
                 record.valid_from.isoformat() if record.valid_from else None,
                 record.valid_until.isoformat() if record.valid_until else None, record.supersedes_id, record.extraction_version),
            )
            self.connection.execute(
                "INSERT INTO memories_fts(memory_id, scope, fact_key, text) VALUES (?, ?, ?, ?)",
                (record.memory_id, record.scope, record.fact_key, record.text),
            )
            if record.supersedes_id:
                cursor = self.connection.execute("UPDATE memories SET status = 'superseded' WHERE memory_id = ? AND scope = ? AND status = 'active'", (record.supersedes_id, record.scope))
                if cursor.rowcount != 1:
                    raise ValueError("superseded memory must name an active record in the same scope")
        return record

    def get(self, memory_id: str) -> MemoryRecord:
        row = self.connection.execute("SELECT * FROM memories WHERE memory_id = ?", (memory_id,)).fetchone()
        if row is None:
            raise KeyError(memory_id)
        return self._record(row)

    def query(self, scope: str, query: str, budget: int = 10) -> list[MemoryRecord]:
        if not scope.strip() or not query.strip() or budget <= 0:
            return []
        terms = [part.lower() for part in query.split() if part.strip()]
        rows = self.connection.execute(
            "SELECT * FROM memories WHERE scope = ? AND status = 'active' ORDER BY observed_at DESC LIMIT ?",
            (scope, max(1, min(budget * 5, 100))),
        ).fetchall()
        now = datetime.now(timezone.utc)
        matches = [self._record(row) for row in rows
                   if self._is_current(row, now)
                   and all(term in (row[3] + " " + row[2]).lower() for term in terms)]
        return matches[: max(1, min(budget, 100))]

    def profile(self, scope: str, budget: int = 12) -> list[MemoryRecord]:
        if budget <= 0:
            return []
        rows = self.connection.execute(
            "SELECT * FROM memories WHERE scope = ? AND status = 'active' ORDER BY fact_key, observed_at DESC LIMIT ?",
            (scope, max(1, min(budget, 100))),
        ).fetchall()
        now = datetime.now(timezone.utc)
        return [self._record(row) for row in rows if self._is_current(row, now)]

    def invalidate(self, source_ref: str) -> int:
        rows = self.connection.execute("SELECT memory_id, source_refs_json FROM memories WHERE status = 'active'").fetchall()
        affected = [memory_id for memory_id, refs in rows if source_ref in json.loads(refs)]
        with self.connection:
            for memory_id in affected:
                self.connection.execute("UPDATE memories SET status = 'stale' WHERE memory_id = ?", (memory_id,))
        return len(affected)

    def forget(self, memory_id: str) -> None:
        with self.connection:
            self.connection.execute("UPDATE memories SET status = 'forgotten' WHERE memory_id = ?", (memory_id,))

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def _is_current(row: tuple, now: datetime) -> bool:
        valid_from = MemoryStore._parse_time(row[9])
        valid_until = MemoryStore._parse_time(row[10])
        return (valid_from is None or valid_from <= now) and (valid_until is None or valid_until > now)

    @staticmethod
    def _parse_time(value: str | None) -> datetime | None:
        if not value:
            return None
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    @staticmethod
    def _record(row: tuple) -> MemoryRecord:
        (_, scope, fact_key, text, evidence_kind, status, refs, hashes, observed, valid_from, valid_until, supersedes_id, version) = row
        return MemoryRecord(
            memory_id=row[0], scope=scope, fact_key=fact_key, text=text, evidence_kind=evidence_kind, status=status,
            source_refs=tuple(json.loads(refs)), supporting_hashes=tuple(json.loads(hashes)),
            observed_at=MemoryStore._parse_time(observed),
            valid_from=MemoryStore._parse_time(valid_from),
            valid_until=MemoryStore._parse_time(valid_until),
            supersedes_id=supersedes_id, extraction_version=version,
        )
