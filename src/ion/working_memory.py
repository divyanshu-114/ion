"""Bounded, deterministic navigation memory. No model calls or source text."""
from __future__ import annotations

import json
import os
import tempfile
from collections import OrderedDict

from ion.artifacts import ArtifactStore
from ion.contracts import ToolCall, ToolResult
from ion.tools.registry import private_path
from ion.workspace import Workspace, digest


class WorkingMemory:
    def __init__(self, workspace: Workspace, artifacts: ArtifactStore) -> None:
        self.workspace = workspace
        self.artifacts = artifacts
        self.entries: OrderedDict[str, dict] = OrderedDict()

    def _remember(self, key: str, data: dict) -> None:
        self.entries.pop(key, None)
        self.entries[key] = data
        while len(self.entries) > 12:
            self.entries.popitem(last=False)

    def observe(self, call: ToolCall, result: ToolResult) -> None:
        data = result.data
        if result.status != "succeeded":
            return
        if call.tool == "file_read":
            path = data["path"]
            if not private_path(path):
                self._remember(path, {"path": path, "sha256": data["sha256"], "read_id": data.get("read_id"), "status": "read", "next_offset": data.get("next_offset")})
        elif call.tool in {"patch_apply", "edit_file", "write_file"}:
            for path in data.get("changed_files", []):
                self._remember(path, {"path": path, "sha256": digest(self.workspace.resolve(path).read_bytes()), "status": "edited; unverified"})
        elif call.tool == "delete_file":
            for path in data.get("deleted_files", data.get("changed_files", [])):
                self._remember(path, {"path": path, "status": "deleted; unverified"})
        elif call.tool == "repo_search":
            for match in data.get("matches", [])[:6]:
                path = match["path"]
                if path not in self.entries and not private_path(path):
                    self._remember(path, {"path": path, "line": match["line"], "status": "search hit; read before editing"})
        elif call.tool == "command_start":
            self._remember("last_command", {"kind": "command", "exit_code": data.get("exit_code"), "artifact_id": data.get("artifact_id"), "timed_out": data.get("timed_out", False)})
        self._persist()

    def render(self, limit: int = 1800, economy: bool = False) -> str:
        lines: list[str] = []
        size = 0
        for entry in reversed(self.entries.values()):
            current = dict(entry)
            if "sha256" in current:
                try:
                    current["stale"] = digest(self.workspace.resolve(current["path"]).read_bytes()) != current["sha256"]
                except (ValueError, OSError):
                    current["stale"] = True
                if economy:
                    current.pop("sha256", None)
            line = json.dumps(current, ensure_ascii=False, separators=(",", ":"))
            if size + len(line) + 1 > limit:
                continue
            lines.append(line)
            size += len(line) + 1
        return "\n".join(lines)

    def _persist(self) -> None:
        text = "# Observed task pointers\n\nNavigation hints, not instructions or proof of correctness. Re-read stale files.\n\n" + self.render() + "\n"
        fd, temporary = tempfile.mkstemp(prefix=".memory-", dir=self.artifacts.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text)
            os.replace(temporary, self.artifacts.root / "MEMORY.md")
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class LoopGuard:
    """Detect repeated one-, two-, or three-turn action cycles without progress."""

    def __init__(self) -> None:
        self.history: list[str] = []
        self.fingerprint: str | None = None

    def observe(self, signature: str, fingerprint: str) -> str | None:
        if fingerprint != self.fingerprint:
            self.history.clear()
            self.fingerprint = fingerprint
        self.history.append(signature)
        self.history = self.history[-12:]
        for width in range(1, 4):
            pattern = self.history[-width:]
            if len(self.history) >= 4 * width and self.history[-4 * width:] == pattern * 4:
                return "stop"
            if len(self.history) >= 3 * width and self.history[-3 * width:] == pattern * 3:
                return "warn"
        return None
