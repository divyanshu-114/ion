from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ion.contracts import EngineEvent, TaskResult, TaskSpec


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunStore:
    """Local, inspectable record of foreground task runs.

    This is a history store, not an automatic resume mechanism. Repository edits
    are never replayed from it.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise ValueError("run database path cannot be a symlink")
        self.connection = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                task_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL,
                task_json TEXT NOT NULL,
                result_json TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                task_id TEXT NOT NULL REFERENCES runs(task_id),
                sequence INTEGER NOT NULL,
                event_json TEXT NOT NULL,
                PRIMARY KEY(task_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS operations (
                task_id TEXT NOT NULL REFERENCES runs(task_id),
                operation_id TEXT NOT NULL,
                tool TEXT NOT NULL,
                intent_json TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(task_id, operation_id)
            );
            CREATE TABLE IF NOT EXISTS request_dedup (
                task_id TEXT NOT NULL REFERENCES runs(task_id),
                request_id TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(task_id, request_id)
            );
            """
        )
        self.connection.commit()

    def begin(self, task: TaskSpec) -> None:
        timestamp = now()
        with self.connection:
            self.connection.execute(
                "INSERT INTO runs(task_id, created_at, updated_at, status, task_json) VALUES (?, ?, ?, ?, ?)",
                (task.task_id, timestamp, timestamp, "running", task.model_dump_json()),
            )

    def append(self, task_id: str, event: EngineEvent) -> int:
        sequence = self.connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        with self.connection:
            self.connection.execute(
                "INSERT INTO events(task_id, sequence, event_json) "
                "VALUES (?, ?, ?)",
                (task_id, sequence, event.model_dump_json()),
            )
            self.connection.execute("UPDATE runs SET updated_at = ? WHERE task_id = ?", (now(), task_id))
        return sequence

    def events_since(self, task_id: str, after_seq: int = 0) -> list[dict]:
        rows = self.connection.execute(
            "SELECT sequence, event_json FROM events WHERE task_id = ? AND sequence > ? ORDER BY sequence",
            (task_id, max(0, after_seq)),
        ).fetchall()
        return [{"seq": sequence, "event": json.loads(event)} for sequence, event in rows]

    def dedupe_request(self, task_id: str, request_id: str, response: dict) -> tuple[bool, dict]:
        """Persist a mutating request response and return (is_new, response)."""
        row = self.connection.execute(
            "SELECT response_json FROM request_dedup WHERE task_id = ? AND request_id = ?",
            (task_id, request_id),
        ).fetchone()
        if row:
            return False, json.loads(row[0])
        with self.connection:
            self.connection.execute(
                "INSERT INTO request_dedup(task_id, request_id, response_json, created_at) VALUES (?, ?, ?, ?)",
                (task_id, request_id, json.dumps(response, sort_keys=True, separators=(",", ":")), now()),
            )
        return True, response

    def set_status(self, task_id: str, status: str) -> None:
        with self.connection:
            self.connection.execute("UPDATE runs SET status = ?, updated_at = ? WHERE task_id = ?", (status, now(), task_id))

    def finish(self, result: TaskResult) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET status = ?, result_json = ?, updated_at = ? WHERE task_id = ?",
                (result.outcome.value, result.model_dump_json(), now(), result.task_id),
            )

    def interrupt(self, task_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET status = 'interrupted', updated_at = ? WHERE task_id = ? AND status = 'running'",
                (now(), task_id),
            )

    def prepare_operation(self, task_id: str, operation_id: str, tool: str, intent: dict) -> bool:
        """Durably record an operation before dispatching its side effect."""
        timestamp = now()
        with self.connection:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO operations(task_id, operation_id, tool, intent_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, 'prepared', ?, ?)",
                (task_id, operation_id, tool, json.dumps(intent, sort_keys=True, separators=(",", ":")), timestamp, timestamp),
            )
        return cursor.rowcount == 1

    def settle_operation(self, task_id: str, operation_id: str, result: dict, status: str) -> None:
        if status not in {"succeeded", "failed", "cancelled", "unknown"}:
            raise ValueError("invalid operation settlement status")
        with self.connection:
            self.connection.execute(
                "UPDATE operations SET status = ?, result_json = ?, updated_at = ? WHERE task_id = ? AND operation_id = ?",
                (status, json.dumps(result, sort_keys=True, separators=(",", ":")), now(), task_id, operation_id),
            )

    def start_operation(self, task_id: str, operation_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE operations SET status = 'running', updated_at = ? WHERE task_id = ? AND operation_id = ? AND status = 'prepared'",
                (now(), task_id, operation_id),
            )

    def unresolved_operations(self, task_id: str) -> list[dict]:
        rows = self.connection.execute(
            "SELECT operation_id, tool, intent_json, status, result_json, created_at, updated_at FROM operations WHERE task_id = ? AND status IN ('prepared', 'running', 'unknown') ORDER BY created_at",
            (task_id,),
        ).fetchall()
        return [
            {"operation_id": op_id, "tool": tool, "intent": json.loads(intent), "status": status,
             "result": json.loads(result) if result else None, "created_at": created, "updated_at": updated}
            for op_id, tool, intent, status, result, created, updated in rows
        ]

    def operation(self, task_id: str, operation_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT operation_id, tool, intent_json, status, result_json, created_at, updated_at FROM operations WHERE task_id = ? AND operation_id = ?",
            (task_id, operation_id),
        ).fetchone()
        if row is None:
            return None
        op_id, tool, intent, status, result, created, updated = row
        return {"operation_id": op_id, "tool": tool, "intent": json.loads(intent), "status": status,
                "result": json.loads(result) if result else None, "created_at": created, "updated_at": updated}

    def recent(self, limit: int = 10) -> list[dict]:
        rows = self.connection.execute(
            "SELECT task_id, created_at, status, task_json, result_json FROM runs ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 100)),),
        ).fetchall()
        return [
            {"task_id": task_id, "created_at": created_at, "status": status,
             "task": json.loads(task_json), "result": json.loads(result_json) if result_json else None}
            for task_id, created_at, status, task_json, result_json in rows
        ]

    def inspect(self, task_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT created_at, updated_at, status, task_json, result_json FROM runs WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        created_at, updated_at, status, task_json, result_json = row
        events = [json.loads(item[0]) for item in self.connection.execute(
            "SELECT event_json FROM events WHERE task_id = ? ORDER BY sequence", (task_id,)
        )]
        return {
            "task_id": task_id, "created_at": created_at, "updated_at": updated_at,
            "status": status, "task": json.loads(task_json),
            "result": json.loads(result_json) if result_json else None,
            "events": events,
        }

    def close(self) -> None:
        self.connection.close()
