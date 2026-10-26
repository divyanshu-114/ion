from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ion.workspace import digest


@dataclass(frozen=True)
class InstructionSource:
    path: str
    text: str
    sha256: str


class InstructionResolver:
    def resolve(self, repo: Path, paths: list[Path]) -> list[InstructionSource]:
        root = repo.resolve()
        directories = {root}
        for path in paths:
            current = (root / path).resolve().parent
            if not current.is_relative_to(root):
                continue
            while current != root:
                directories.add(current)
                current = current.parent
        sources: list[InstructionSource] = []
        for directory in sorted(directories, key=lambda item: (len(item.parts), str(item))):
            for name in ("AGENTS.md", "CLAUDE.md"):
                candidate = directory / name
                if candidate.is_file() and not candidate.is_symlink():
                    data = candidate.read_bytes()
                    sources.append(InstructionSource(candidate.relative_to(root).as_posix(), data.decode("utf-8", "replace"), digest(data)))
                    break
        return sources
