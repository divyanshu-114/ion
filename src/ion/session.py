"""Durable session projection and private JSONL control transport."""

from __future__ import annotations

import asyncio
import json
import os
import hashlib
import tempfile
from pathlib import Path

from ion.contracts import EngineEvent, Outcome, TaskResult, TaskSpec
from ion.storage import RunStore


class SessionService:
    """Single-writer façade over :class:`RunStore`.

    The TUI can disconnect and reconstruct its view from ``subscribe`` without
    owning the engine loop. Mutating requests carry a request ID and are
    persisted before their acknowledgement is returned.
    """

    def __init__(self, store: RunStore) -> None:
        self.store = store

    def start(self, task: TaskSpec) -> str:
        self.store.begin(task)
        return task.task_id

    def append(self, session_id: str, event: EngineEvent) -> int:
        return self.store.append(session_id, event)

    def subscribe(self, session_id: str, after_seq: int = 0) -> list[dict]:
        return self.store.events_since(session_id, after_seq)

    def inspect(self, session_id: str) -> dict | None:
        detail = self.store.inspect(session_id)
        if detail is not None:
            detail["last_seq"] = self.store.events_since(session_id)[-1]["seq"] if self.store.events_since(session_id) else 0
            detail["unresolved_operations"] = self.store.unresolved_operations(session_id)
        return detail

    def result(self, session_id: str) -> dict | None:
        detail = self.store.inspect(session_id)
        return detail.get("result") if detail else None

    def control(self, session_id: str, action: str, request_id: str) -> dict:
        if action not in {"pause", "resume", "cancel"}:
            raise ValueError("unsupported session action")
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id cannot be blank")
        existing = self.store.connection.execute(
            "SELECT response_json FROM request_dedup WHERE task_id = ? AND request_id = ?",
            (session_id, request_id),
        ).fetchone()
        if existing:
            return json.loads(existing[0])
        detail = self.store.inspect(session_id)
        if detail is None:
            return {"ok": False, "error": "session not found"}
        current = detail["status"]
        unresolved = self.store.unresolved_operations(session_id)
        if action == "resume" and unresolved:
            response = {"ok": False, "error": "reconciliation required", "operations": unresolved}
        elif action == "pause" and current != "running":
            response = {"ok": False, "error": f"cannot pause session in {current} state"}
        elif action == "resume" and current != "paused":
            response = {"ok": False, "error": f"cannot resume session in {current} state"}
        elif action == "cancel" and current not in {"running", "paused"}:
            response = {"ok": False, "error": f"cannot cancel session in {current} state"}
        else:
            status = {"pause": "paused", "resume": "running", "cancel": "cancelled"}[action]
            self.store.set_status(session_id, status)
            seq = self.store.append(session_id, EngineEvent(phase="act", message=f"session {action} requested"))
            response = {"ok": True, "status": status, "seq": seq}
        return self.store.dedupe_request(session_id, request_id, response)[1]

    def handle(self, method: str, params: dict) -> dict:
        if method == "inspect":
            detail = self.inspect(params["session_id"])
            return detail or {"error": "session not found"}
        if method == "subscribe":
            return {"events": self.subscribe(params["session_id"], params.get("after_seq", 0))}
        if method == "get_result":
            result = self.result(params["session_id"])
            return {"result": result} if result else {"error": "not_finished"}
        if method in {"pause", "resume", "cancel"}:
            return self.control(params["session_id"], method, params["request_id"])
        raise ValueError("unknown session method")


class SessionSocketServer:
    """Small owner-only JSONL server for reconnectable session inspection."""

    def __init__(self, service: SessionService, socket_path: Path) -> None:
        self.service = service
        self.socket_path = socket_path
        self.server: asyncio.AbstractServer | None = None

    @staticmethod
    def compact_path(data_root: Path) -> Path:
        """Return a private short path that fits macOS/Linux AF_UNIX limits."""
        key = hashlib.sha256(str(Path(data_root).resolve()).encode()).hexdigest()[:20]
        return Path(tempfile.gettempdir()) / f"ion-{key}.sock"

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.socket_path.exists():
            self.socket_path.unlink()
        self.server = await asyncio.start_unix_server(self._client, path=str(self.socket_path))
        os.chmod(self.socket_path, 0o600)

    async def close(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        self.socket_path.unlink(missing_ok=True)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                if len(raw) > 1024 * 1024:
                    raise ValueError("request frame exceeds 1 MiB")
                request = json.loads(raw)
                request_id = request.get("request_id")
                try:
                    result = self.service.handle(request["method"], request.get("params", {}))
                    response = {"request_id": request_id, "result": result}
                except (KeyError, TypeError, ValueError) as exc:
                    response = {"request_id": request_id, "error": {"category": "validation", "message": str(exc)}}
                writer.write((json.dumps(response, separators=(",", ":")) + "\n").encode())
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
