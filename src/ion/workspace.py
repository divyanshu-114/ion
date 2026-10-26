from __future__ import annotations

import hashlib
import difflib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class ChangeSet:
    changed_files: tuple[str, ...]
    attributable_files: tuple[str, ...]
    external_files: tuple[str, ...]
    ambiguous_files: tuple[str, ...]


class Workspace:
    GENERATED_DIRS = {".git", ".pytest_cache", ".mypy_cache", ".ruff_cache", "__pycache__", ".venv", ".uv-cache", ".uv-tools", "node_modules"}
    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("repository path must be a directory")
        self.baseline = self._snapshot()
        self.writes: dict[str, tuple[str | None, str | None]] = {}
        self.write_contents: dict[str, tuple[bytes, bytes]] = {}

    @classmethod
    def capture(cls, root: Path) -> Workspace:
        return cls(root)

    def resolve(self, relative_path: str, allow_new: bool = False) -> Path:
        candidate = Path(relative_path)
        if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
            raise ValueError("path escapes repository")
        path = self.root.joinpath(candidate)
        cursor = self.root
        for component in candidate.parts:
            cursor /= component
            if cursor.is_symlink():
                raise ValueError("symlink paths are unsupported")
        parent = path.parent.resolve(strict=not allow_new)
        if not parent.is_relative_to(self.root):
            raise ValueError("path escapes repository")
        if path.is_symlink():
            raise ValueError("symlink files are unsupported")
        if not allow_new and (not path.is_file() or not path.resolve().is_relative_to(self.root)):
            raise ValueError("file does not exist")
        return path

    def _paths(self) -> list[Path]:
        try:
            result = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True)
            names = [name.decode("utf-8", "surrogateescape") for name in result.stdout.split(b"\0") if name]
            candidates = [self.root / name for name in names]
        except (OSError, subprocess.CalledProcessError):
            candidates = list(self.root.rglob("*"))
        paths = []
        for path in candidates:
            relative = path.relative_to(self.root)
            if any(part in self.GENERATED_DIRS for part in relative.parts):
                continue
            try:
                paths.append(self.resolve(relative.as_posix()))
            except (ValueError, OSError):
                continue
        return paths

    def _snapshot(self) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        for path in self._paths():
            try:
                snapshot[path.relative_to(self.root).as_posix()] = digest(path.read_bytes())
            except OSError:
                continue
        return snapshot

    def fingerprint(self) -> str:
        import json
        return digest(json.dumps(self._snapshot(), sort_keys=True).encode())

    def changes(self) -> ChangeSet:
        current = self._snapshot()
        changed = tuple(sorted(path for path in set(self.baseline) | set(current) if self.baseline.get(path) != current.get(path)))
        attributable: list[str] = []
        ambiguous: list[str] = []
        external: list[str] = []
        for path in changed:
            if path not in self.writes:
                external.append(path)
            elif self.writes[path][1] == current.get(path):
                attributable.append(path)
            else:
                ambiguous.append(path)
        return ChangeSet(changed, tuple(attributable), tuple(external), tuple(ambiguous))

    def record_write(self, path: str, before_hash: str | None, after_hash: str | None, before: bytes, after: bytes) -> None:
        initial_hash = self.writes[path][0] if path in self.writes else before_hash
        initial_bytes = self.write_contents[path][0] if path in self.write_contents else before
        self.writes[path] = (initial_hash, after_hash)
        self.write_contents[path] = (initial_bytes, after)

    def patch_text(self) -> str:
        changes = self.changes()
        chunks: list[str] = []
        for path in changes.attributable_files:
            before, after = self.write_contents[path]
            chunks.extend(difflib.unified_diff(
                before.decode("utf-8", "replace").splitlines(keepends=True),
                after.decode("utf-8", "replace").splitlines(keepends=True),
                fromfile=f"a/{path}", tofile=f"b/{path}",
            ))
        return "".join(chunks)
