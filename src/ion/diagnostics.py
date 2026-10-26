from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class DiagnosticLogger:
    """Best-effort structured diagnostics with no prompts, source text, or keys."""

    def __init__(self, path: Path, task_id: str, max_bytes: int = 2_000_000) -> None:
        self.path = path
        self.task_id = task_id
        self.max_bytes = max_bytes

    def emit(self, event: str, **fields: Any) -> None:
        record = {
            "time": datetime.now(timezone.utc).isoformat(),
            "task_id": self.task_id,
            "event": event,
            **fields,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                rotated = self.path.with_suffix(".previous.jsonl")
                rotated.unlink(missing_ok=True)
                self.path.replace(rotated)
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError:
            # Diagnostics must never stop a task.
            return


def recent_diagnostics(path: Path, limit: int = 80) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-max(1, min(limit, 500)):]
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            records.append(value)
    return records
