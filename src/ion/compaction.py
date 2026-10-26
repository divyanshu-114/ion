from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from ion.contracts import ContextCheckpoint


class Compactor:
    REQUIRED_SECTIONS = ("Objective:", "Constraints:", "Completed:", "Active:", "Blockers:", "Next actions:")

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def compact(
        self,
        session_id: str,
        through_seq: int,
        summary: str,
        *,
        task_text: str,
        constraints: tuple[str, ...] = (),
        amendment_version: int = 0,
        model_profile_digest: str,
        recent_event_refs: tuple[str, ...] = (),
        pinned_evidence_refs: tuple[str, ...] = (),
    ) -> ContextCheckpoint:
        if not summary.strip():
            raise ValueError("compaction summary cannot be blank")
        missing = [section for section in self.REQUIRED_SECTIONS if section not in summary]
        if missing:
            raise ValueError("required section missing: " + ", ".join(missing))
        if len(summary) > 12000:
            raise ValueError("compaction summary exceeds bounded size")
        effective = tuple(item.strip() for item in constraints if item.strip())
        pinned = "Task: " + task_text.strip() + "\nEffective constraints: " + ("; ".join(effective) or "none")
        full_summary = pinned + "\n" + summary.strip()
        if len(full_summary) > 12000:
            raise ValueError("compaction checkpoint exceeds bounded size")
        digest = hashlib.sha256("\n".join(effective).encode()).hexdigest()
        checkpoint = ContextCheckpoint(
            session_id=session_id,
            through_seq=through_seq,
            amendment_version=amendment_version,
            constraints_digest=digest,
            summary=full_summary,
            recent_event_refs=recent_event_refs,
            pinned_evidence_refs=pinned_evidence_refs,
            model_profile_digest=model_profile_digest,
        )
        self._commit(checkpoint)
        return checkpoint

    def load(self, session_id: str) -> ContextCheckpoint | None:
        path = self.root / f"{session_id}.json"
        if path.is_symlink():
            raise ValueError("context checkpoint cannot be a symlink")
        if not path.is_file():
            return None
        try:
            return ContextCheckpoint.model_validate_json(path.read_text())
        except (OSError, ValueError):
            raise ValueError("context checkpoint is missing or corrupt")

    def _commit(self, checkpoint: ContextCheckpoint) -> None:
        path = self.root / f"{checkpoint.session_id}.json"
        fd, temporary = tempfile.mkstemp(prefix=".checkpoint-", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(checkpoint.model_dump_json())
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            Path(temporary).replace(path)
        finally:
            Path(temporary).unlink(missing_ok=True)
