from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from uuid import uuid4

from ion.contracts import ArtifactRef


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._metadata_root = self.root / ".metadata"
        self._metadata_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def put(self, data: bytes, kind: str, redacted: bool = False, complete: bool = True) -> ArtifactRef:
        artifact_id = str(uuid4())
        path = self.root / artifact_id
        fd, temp_name = tempfile.mkstemp(prefix=f".{artifact_id}-", suffix=".tmp", dir=self.root)
        os.close(fd)
        temp = Path(temp_name)
        metadata_path = self._metadata_root / artifact_id
        metadata_fd, metadata_temp_name = tempfile.mkstemp(prefix=f".{artifact_id}-", suffix=".tmp", dir=self._metadata_root)
        os.close(metadata_fd)
        metadata_temp = Path(metadata_temp_name)
        try:
            with temp.open("wb") as stream:
                os.chmod(temp, 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            with metadata_temp.open("wb") as stream:
                os.chmod(metadata_temp, 0o600)
                stream.write(b"1" if complete else b"0")
                stream.flush()
                os.fsync(stream.fileno())
            metadata_temp.replace(metadata_path)
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
            metadata_temp.unlink(missing_ok=True)
        return ArtifactRef(
            artifact_id=artifact_id,
            kind=kind,
            relative_store_path=artifact_id,
            sha256=hashlib.sha256(data).hexdigest(),
            byte_count=len(data),
            complete=complete,
            redaction_applied=redacted,
        )

    def read(self, artifact_id: str) -> bytes:
        if "/" in artifact_id or ".." in artifact_id:
            raise ValueError("invalid artifact id")
        path = self.root / artifact_id
        if path.is_symlink():
            raise ValueError("artifact cannot be a symlink")
        return path.read_bytes()

    def is_complete(self, artifact_id: str) -> bool:
        if "/" in artifact_id or ".." in artifact_id:
            raise ValueError("invalid artifact id")
        metadata_path = self._metadata_root / artifact_id
        if metadata_path.is_symlink():
            raise ValueError("artifact metadata cannot be a symlink")
        try:
            state = metadata_path.read_bytes()
        except FileNotFoundError:
            return False  # Legacy artifacts cannot prove complete capture.
        if state not in (b"0", b"1"):
            raise ValueError("invalid artifact completeness metadata")
        return state == b"1"

    def has_artifacts(self) -> bool:
        return any(path.is_file() and path.name not in {"MEMORY.md", ".metadata"}
                   for path in self.root.iterdir())
