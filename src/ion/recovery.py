"""Workspace admission and conservative crash recovery primitives."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from uuid import uuid4


class WorkspaceRecoveryRequired(RuntimeError):
    """Raised when a prior owner may have left an unresolved side effect."""


class WorkspaceLease:
    """Admit one Ion writer to a workspace with a durable conservative claim.

    The OS lock prevents concurrent writers in the normal case. The registry
    survives a process crash, so releasing the OS lock alone never grants a new
    writer access to an owner marked active or recovery_required.
    """

    def __init__(self, workspace: Path, registry_path: Path, session_id: str | None = None) -> None:
        self.workspace = Path(workspace).resolve(strict=True)
        self.registry_path = Path(registry_path)
        self.registry_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_path = self.registry_path.with_name(self.registry_path.name + ".lock")
        self.session_id = session_id or str(uuid4())
        self.engine_identity = str(uuid4())
        self._lock = None
        self._held = False

    @staticmethod
    def workspace_id(workspace: Path) -> str:
        return hashlib.sha256(str(Path(workspace).resolve(strict=True)).encode()).hexdigest()

    def acquire(self) -> "WorkspaceLease":
        if self._held:
            return self
        if self.lock_path.is_symlink():
            raise WorkspaceRecoveryRequired("workspace lock path cannot be a symlink")
        self.lock_path.touch(mode=0o600, exist_ok=True)
        os.chmod(self.lock_path, 0o600)
        self._lock = self.lock_path.open("r+")
        try:
            self._flock(exclusive=True, nonblocking=True)
        except Exception:
            self._close_lock()
            raise WorkspaceRecoveryRequired("workspace is already owned by another Ion session")

        current = self._read_claim()
        if current:
            status = current.get("status")
            if status in {"active", "recovery_required"}:
                if status == "active" and not self._pid_alive(current.get("engine_pid")):
                    current = {**current, "status": "recovery_required"}
                    self._write_claim(current)
                self._close_lock()
                raise WorkspaceRecoveryRequired(
                    f"workspace recovery required for session {current.get('owner_session_id', 'unknown')}"
                )
            if status != "clean":
                self._close_lock()
                raise WorkspaceRecoveryRequired("workspace ownership registry has an unknown state")
            if current.get("workspace_id") != self.workspace_id(self.workspace):
                self._close_lock()
                raise WorkspaceRecoveryRequired("workspace ownership registry identity mismatch")
        claim = {
            "workspace_id": self.workspace_id(self.workspace),
            "owner_session_id": self.session_id,
            "engine_identity": self.engine_identity,
            "engine_pid": os.getpid(),
            "engine_started_at": time.time(),
            "status": "active",
            "pending_operation_refs": [],
        }
        self._write_claim(claim)
        self._held = True
        return self

    def mark_recovery_required(self, pending_operation_refs: list[str] | tuple[str, ...] = ()) -> None:
        if not self._held:
            raise RuntimeError("workspace lease is not held")
        claim = self._read_claim() or {}
        claim.update(status="recovery_required", pending_operation_refs=list(pending_operation_refs))
        self._write_claim(claim)

    def release(self) -> None:
        if not self._held:
            self._close_lock()
            return
        claim = self._read_claim() or {}
        if claim.get("owner_session_id") == self.session_id and claim.get("status") != "recovery_required":
            claim.update(status="clean", pending_operation_refs=[])
            self._write_claim(claim)
        self._held = False
        self._close_lock()

    def __enter__(self) -> "WorkspaceLease":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def _read_claim(self) -> dict | None:
        if not self.registry_path.exists():
            return None
        try:
            raw = self.registry_path.read_text()
            value = json.loads(raw) if raw else None
            if value is not None and not isinstance(value, dict):
                raise WorkspaceRecoveryRequired("workspace ownership registry is not an object")
            return value
        except (OSError, ValueError, TypeError) as exc:
            raise WorkspaceRecoveryRequired("workspace ownership registry is corrupt") from exc

    def _write_claim(self, claim: dict) -> None:
        fd, name = tempfile.mkstemp(prefix=".ion-ownership-", dir=self.registry_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(claim, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(name, 0o600)
            Path(name).replace(self.registry_path)
        finally:
            Path(name).unlink(missing_ok=True)

    @staticmethod
    def _pid_alive(pid: object) -> bool:
        if not isinstance(pid, int) or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _flock(self, exclusive: bool, nonblocking: bool) -> None:
        import fcntl
        flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if nonblocking:
            flags |= fcntl.LOCK_NB
        fcntl.flock(self._lock.fileno(), flags)

    def _close_lock(self) -> None:
        if self._lock is None:
            return
        try:
            import fcntl
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
        finally:
            self._lock.close()
            self._lock = None
