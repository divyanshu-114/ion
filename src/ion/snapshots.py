from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    root_hash: str
    files: tuple[str, ...]
    byte_count: int


class SnapshotStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def capture(self, workspace: Path, relative_paths: list[str] | tuple[str, ...], quota_bytes: int = 8 * 1024 * 1024) -> Snapshot:
        base = Path(workspace).resolve(strict=True)
        blobs: dict[str, bytes] = {}
        total = 0
        for relative in relative_paths:
            path = (base / relative).resolve(strict=True)
            if not path.is_file() or not path.is_relative_to(base):
                raise ValueError("snapshot path escapes workspace")
            data = path.read_bytes()
            total += len(data)
            if total > quota_bytes:
                raise ValueError("snapshot quota exceeded")
            blobs[Path(relative).as_posix()] = data
        snapshot_id = str(uuid4())
        manifest = {"workspace": str(base), "files": {path: hashlib.sha256(data).hexdigest() for path, data in blobs.items()}}
        self._atomic_write(snapshot_id, "manifest.json", json.dumps(manifest, sort_keys=True).encode())
        for path, data in blobs.items():
            self._atomic_write(snapshot_id, path, data)
        root_hash = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        return Snapshot(snapshot_id, root_hash, tuple(sorted(blobs)), total)

    def read(self, snapshot_id: str, relative_path: str) -> bytes:
        if "/" in snapshot_id or ".." in snapshot_id:
            raise ValueError("invalid snapshot id")
        path = self.root / snapshot_id / Path(relative_path)
        if not path.resolve().is_relative_to((self.root / snapshot_id).resolve()):
            raise ValueError("snapshot path escapes")
        return path.read_bytes()

    def _atomic_write(self, snapshot_id: str, relative: str, data: bytes) -> None:
        directory = self.root / snapshot_id / Path(relative).parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.root / snapshot_id / relative
        fd, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            Path(temporary).replace(target)
        finally:
            Path(temporary).unlink(missing_ok=True)
