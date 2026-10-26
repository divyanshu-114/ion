from __future__ import annotations

import asyncio
import os
import signal
import shlex
import re
from pathlib import Path
from uuid import uuid4

from ion.artifacts import ArtifactStore
from ion.redaction import redact
from ion.workspace import Workspace


class CommandSupervisor:
    def __init__(self, workspace: Workspace, artifacts: ArtifactStore, credential: str = "") -> None:
        self.workspace = workspace
        self.artifacts = artifacts
        self.credential = credential
        self.active: dict[str, asyncio.subprocess.Process] = {}

    def _environment(self) -> dict[str, str]:
        allowed = ("PATH", "LANG", "LC_ALL", "TERM", "TMPDIR", "VIRTUAL_ENV", "PYTHONPATH", "PYTHONUNBUFFERED")
        return {key: value for key, value in os.environ.items() if key in allowed}

    async def run(self, command: str, timeout_seconds: float = 120) -> dict:
        if not command.strip():
            raise ValueError("command cannot be blank")
        self._validate_command(command)
        timeout_seconds = min(max(timeout_seconds, 0.1), 120)
        handle = str(uuid4())
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=self.workspace.root,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self._environment(),
            start_new_session=True,
        )
        self.active[handle] = process
        retained = bytearray()
        discarded = 0

        async def drain() -> None:
            nonlocal discarded
            assert process.stdout is not None
            while chunk := await process.stdout.read(65536):
                allowed = max(0, 16 * 1024 * 1024 - len(retained))
                retained.extend(chunk[:allowed])
                discarded += len(chunk) - allowed

        drain_task = asyncio.create_task(drain())
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout_seconds)
        except asyncio.TimeoutError:
            timed_out = True
            await self._terminate(process)
        except asyncio.CancelledError:
            await self._terminate(process)
            raise
        finally:
            await drain_task
            self.active.pop(handle, None)
        output = redact(retained.decode("utf-8", "replace"), (self.credential,))
        artifact = self.artifacts.put(output.encode(), "command_output", redacted=bool(self.credential), complete=discarded == 0)
        lines = output.splitlines()
        preview = "\n".join(lines[:100] + (["…"] if len(lines) > 200 else []) + lines[-100:] if len(lines) > 200 else lines)
        return {
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "output": preview[:8000],
            "artifact_id": artifact.artifact_id,
            "discarded_bytes": discarded,
            "lossy": discarded > 0,
        }

    @staticmethod
    def _validate_command(command: str) -> None:
        if "\x00" in command:
            raise ValueError("command execution policy rejected null bytes")
        if "\n" in command or "\r" in command:
            raise ValueError("command execution policy rejects multiline shell input")
        if re.search(r"\$\(|`|\$\{|<\(|>\(", command):
            raise ValueError("command execution policy rejects shell substitutions")
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            parts = list(lexer)
        except ValueError as exc:
            raise ValueError("command execution policy rejected malformed shell syntax") from exc
        if any(part in {"&&", "||", ";", "|", ">", ">>", "<", "&"} for part in parts):
            raise ValueError("command execution policy rejects shell chains and redirection")
        lowered = command.lower()
        if re.search(r"\b(?:sudo|rm\s+-rf|git\s+(?:reset\s+--hard|clean\b|checkout\b|restore\b|push\b)|mkfs|shutdown|reboot)\b", lowered):
            raise ValueError("command execution policy rejected destructive command")

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), 3)
        except asyncio.TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()

    async def close(self) -> None:
        await asyncio.gather(*(self._terminate(process) for process in tuple(self.active.values())))
