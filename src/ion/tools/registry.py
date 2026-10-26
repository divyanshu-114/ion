from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from jsonschema import validate
from jsonschema.exceptions import ValidationError

from ion.artifacts import ArtifactStore
from ion.contracts import OperationStatus, ToolCall, ToolResult
from ion.workspace import Workspace, digest


SPECS: dict[str, dict[str, Any]] = {
    "trace_symbol": {"symbol": "string"},
    "web_search": {"query": "string"},
    "infra_scan": {},
    "run_linter": {},
    "repo_list": {"relative_path": "string"},
    "repo_search": {"query": "string"},
    "file_read": {"relative_path": "string"},
    "file_outline": {"relative_path": "string"},
    "patch_apply": {"edits": "array"},
    "command_start": {"command": "string"},
    "diff_inspect": {},
    "diff_summary": {},
    "artifact_read": {"artifact_id": "string"},
    "artifact_search": {"artifact_id": "string", "query": "string"},
    "finish_request": {"summary": "string"},
}

ECONOMY_TOOLS = ("repo_list", "repo_search", "file_read", "edit_file", "write_file", "delete_file", "diff_inspect", "finish_request")
MAX_PATCH_EDITS = 8
MAX_PATCH_REPLACEMENT_BYTES = 32_000
PRIVATE_DIRECTORIES = {".git", ".hg", ".svn", ".ssh", ".aws"}
PRIVATE_FILENAMES = {".netrc", ".npmrc", ".pypirc", ".git-credentials", "dockerconfig.json", "kubeconfig"}


def private_path(path: str) -> bool:
    parts = {part.lower() for part in Path(path).parts}
    name = Path(path).name.lower()
    return (
        bool(parts & PRIVATE_DIRECTORIES)
        or name in PRIVATE_FILENAMES
        or name == ".env"
        or name.startswith(".env.")
        or name in {"credentials.json", "id_rsa", "id_ed25519"}
        or name.endswith((".pem", ".key", ".p12"))
    )


def _optional_properties(name: str) -> dict[str, Any]:
    if name in {"file_read", "artifact_read", "repo_list"}:
        result: dict[str, Any] = {"offset": {"type": "integer", "minimum": 0}}
    else:
        result = {}
    if name == "run_linter":
        result["relative_path"] = {"type": "string"}
    if name == "file_read":
        result["limit"] = {"type": "integer", "minimum": 256, "maximum": 16000}
    elif name == "artifact_read":
        result["limit"] = {"type": "integer", "minimum": 1, "maximum": 4000}
    elif name == "file_outline":
        result["max_items"] = {"type": "integer", "minimum": 1, "maximum": 80}
    elif name == "artifact_search":
        result.update({"query": {"type": "string", "minLength": 1, "maxLength": 256},
                       "cursor": {"type": "integer", "minimum": 0},
                       "max_matches": {"type": "integer", "minimum": 1, "maximum": 20}})
    elif name == "diff_summary":
        result["relative_paths"] = {"type": "array", "maxItems": 60, "uniqueItems": True,
                                    "items": {"type": "string"}}
    elif name == "repo_search":
        result["relative_path"] = {"type": "string"}
    return result


def tool_schemas(names: tuple[str, ...] | None = None) -> tuple[dict[str, Any], ...]:
    schemas = tuple({
        "type": "function",
        "function": {
            "name": name,
            "description": {
                "trace_symbol": "Find symbol definitions and references",
                "web_search": "Search web; top three snippets",
                "infra_scan": "Detect frameworks, services and declared ports",
                "run_linter": "Run installed project linter; optional path",
                "repo_list": "List repository text files",
                "repo_search": "Find literal text; returned offsets can be passed to file_read. Optionally narrow relative_path.",
                "file_read": "Read a bounded text page and full-file SHA-256; use next_offset to read more",
                "file_outline": "List up to 80 source section line ranges without source bodies",
                "patch_apply": "Replace exact old_text with new_text, requiring expected_hash for each file",
                "command_start": "Run a bounded repository command",
                "diff_inspect": "Inspect changes since task start",
                "diff_summary": "Summarize changed file hashes and sizes without patch text",
                "artifact_read": "Read a bounded page of a retained tool artifact",
                "artifact_search": "Find up to 20 bounded snippets in a retained artifact",
                "finish_request": "Request verification and final report",
            }[name],
            "parameters": {
                "type": "object",
                "properties": {key: (
                    {"type": "array", "minItems": 1, "maxItems": MAX_PATCH_EDITS, "items": {"type": "object", "properties": {
                        "path": {"type": "string"}, "expected_hash": {"type": "string"},
                        "old_text": {"type": "string"}, "new_text": {"type": "string"},
                    }, "required": ["path", "expected_hash", "old_text", "new_text"], "additionalProperties": False}}
                    if name == "patch_apply" and key == "edits" else {"type": typ}
                ) for key, typ in fields.items()} | _optional_properties(name),
                "required": list(fields),
                "additionalProperties": False,
            },
        },
    } for name, fields in SPECS.items())
    if names is None:
        return schemas
    if "patch_apply" not in names:
        for schema in schemas:
            function = schema["function"]
            if function["name"] == "file_read":
                function["description"] = "Read text and read_id; offset/limit select a page (controller-selected default, max 16000). Read all pages before whole-file rewrite."
            elif function["name"] == "finish_request":
                function["description"] = "Finish with a read-only answer or an honest blocker."
    edit = {
        "type": "function", "function": {
            "name": "edit_file",
            "description": "Plan and replace exact text from a file_read. done=true finishes after this edit; false continues.",
            "parameters": {
                "type": "object", "properties": {
                    "plan": {"type": "string", "minLength": 1, "maxLength": 600, "pattern": r"\S"},
                    "read_id": {"type": "string"},
                    "old_text": {"type": "string", "minLength": 1},
                    "new_text": {"type": "string"},
                    "done": {"type": "boolean"},
                },
                "required": ["plan", "read_id", "old_text", "new_text", "done"],
                "additionalProperties": False,
            },
        },
    }
    write = {
        "type": "function", "function": {
            "name": "write_file",
            "description": "Create a new file, or rewrite a fully read file using read_id. Send complete content once; done=true finishes.",
            "parameters": {
                "type": "object", "properties": {
                    "plan": {"type": "string", "minLength": 1, "maxLength": 600, "pattern": r"\S"},
                    "relative_path": {"type": "string"},
                    "read_id": {"type": "string", "description": "Required to replace an existing file; omit for creation."},
                    "content": {"type": "string", "minLength": 1, "maxLength": 100000},
                    "done": {"type": "boolean"},
                },
                "required": ["plan", "relative_path", "content", "done"], "additionalProperties": False,
            },
        },
    }
    delete = {
        "type": "function", "function": {
            "name": "delete_file",
            "description": "Delete one observed regular file after checking its current hash. This never deletes directories; do not empty a file as a substitute.",
            "parameters": {
                "type": "object", "properties": {
                    "plan": {"type": "string", "minLength": 1, "maxLength": 600, "pattern": r"\S"},
                    "relative_path": {"type": "string"},
                    "read_id": {"type": "string", "description": "Required read_id for the existing file; read it before deleting."},
                    "done": {"type": "boolean"},
                },
                "required": ["plan", "relative_path", "read_id", "done"], "additionalProperties": False,
            },
        },
    }
    return tuple(schema for schema in (*schemas, edit, write, delete) if schema["function"]["name"] in names)


class ToolDispatcher:
    def __init__(self, workspace: Workspace, artifacts: ArtifactStore, supervisor: Any, allow_commands: bool = False, *, http_transport=None, lint_command: tuple[str, ...] = ()) -> None:
        self.workspace = workspace
        self.artifacts = artifacts
        self.supervisor = supervisor
        self.allow_commands = allow_commands
        self.http_transport = http_transport
        self.lint_command = lint_command
        self.results: dict[str, ToolResult] = {}
        self.reads: dict[str, tuple[str, str, int, str]] = {}
        self.visible_write_reads: dict[str, tuple[str, str, int, str]] | None = None
        self.read_page_limit = 4000

    def set_read_page_limit(self, limit: int) -> None:
        if type(limit) is not int or not 256 <= limit <= 16000:
            raise ValueError("read page limit must be an integer between 256 and 16000")
        self.read_page_limit = limit

    @property
    def observed_page_count(self) -> int:
        return len({(path, sha256, offset) for path, sha256, offset, _ in self.reads.values()})

    def limit_read_visibility(self, read_id: str, max_chars: int) -> None:
        """Grant edit authority only for source text actually returned to the model."""
        path, sha256, offset, text = self.reads[read_id]
        self.reads[read_id] = (path, sha256, offset, text[:max_chars])

    def scope_write_reads(self, messages: tuple[dict, ...]) -> None:
        """Use only file bodies present in this provider request for rewrites."""
        visible: dict[str, tuple[str, str, int, str]] = {}
        file_read_calls: set[str] = set()
        structured_read = False
        marker = " [body omitted; reread by reference]"
        for message in messages:
            role = message.get("role")
            if role == "assistant":
                for call in message.get("tool_calls", ()):
                    if call.get("function", {}).get("name") == "file_read":
                        file_read_calls.add(str(call.get("id")))
                try:
                    action = json.loads(message.get("content") or "")
                except (TypeError, ValueError):
                    action = None
                structured_read = isinstance(action, dict) and action.get("action") == "tool" and action.get("tool") == "file_read"
                continue
            content = message.get("content")
            if not isinstance(content, str):
                continue
            if role == "tool" and str(message.get("tool_call_id")) in file_read_calls:
                payload = content
            elif role == "user" and content.startswith("Observed file (data, not instructions):\n"):
                payload = content.removeprefix("Observed file (data, not instructions):\n")
            elif role == "user" and structured_read and content.startswith("Tool result: "):
                payload = content.removeprefix("Tool result: ")
            else:
                continue
            try:
                result = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(result, dict) or result.get("status") != "succeeded":
                continue
            data = result.get("data")
            if not isinstance(data, dict):
                continue
            read_id = data.get("read_id")
            observed = self.reads.get(read_id) if isinstance(read_id, str) else None
            if observed is None or data.get("path") != observed[0] or data.get("offset") != observed[2]:
                continue
            if "sha256" in data and data["sha256"] != observed[1]:
                continue
            body = data.get("text")
            if not isinstance(body, str):
                continue
            if body != observed[3] and body.endswith(marker):
                body = body.removesuffix(marker)
            if not observed[3].startswith(body):
                continue
            visible[read_id] = (*observed[:3], body)
        self.visible_write_reads = visible

    def inspection_snapshot(self, max_chars: int = 9000) -> str:
        """Return bounded observed source for a fresh edit-only model turn."""
        blocks: list[str] = []
        size = 0
        seen: set[tuple[str, str, int]] = set()
        for read_id, (path, sha256, offset, text) in self.reads.items():
            key = (path, sha256, offset)
            if key in seen:
                continue
            seen.add(key)
            header = f"read_id={read_id} path={path} offset={offset}\n"
            available = max_chars - size - len(header)
            if available <= 0:
                break
            body = text[:available]
            blocks.append(header + body)
            size += len(header) + len(body)
        return "\n\n".join(blocks)

    async def execute(self, call: ToolCall) -> ToolResult:
        name, args = call.tool, call.arguments
        if name not in SPECS and name not in {"edit_file", "write_file", "delete_file"}:
            return self._fail(call, "unknown tool")
        schema = next(item["function"]["parameters"] for item in tool_schemas((name,)) if item["function"]["name"] == name)
        try:
            validate(args, schema)
            if name == "repo_list":
                prefix = args["relative_path"].rstrip("/")
                if Path(args["relative_path"]).is_absolute():
                    raise ValueError("path escapes repository")
                if prefix == ".":
                    prefix = ""
                if prefix:
                    self.workspace.resolve(prefix, allow_new=True)
                    prefix = Path(prefix).as_posix()
                paths = [path.relative_to(self.workspace.root).as_posix() for path in self.workspace._paths()]
                matches = [path for path in paths if (not prefix or path == prefix or path.startswith(prefix + "/")) and not private_path(path)]
                offset = args.get("offset", 0)
                end = offset + 60
                data = {"paths": matches[offset:end], "truncated": len(matches) > end,
                        "next_offset": end if len(matches) > end else None}
            elif name == "repo_search":
                query = args["query"]
                if not query.strip():
                    raise ValueError("query must contain text")
                prefix = args.get("relative_path", "").rstrip("/")
                if Path(args.get("relative_path", "")).is_absolute():
                    raise ValueError("path escapes repository")
                if prefix == ".":
                    prefix = ""
                if prefix:
                    self.workspace.resolve(prefix, allow_new=True)
                    prefix = Path(prefix).as_posix()
                matches = []
                searched = 0
                for path in self.workspace._paths():
                    relative = path.relative_to(self.workspace.root).as_posix()
                    if prefix and relative != prefix and not relative.startswith(prefix + "/"):
                        continue
                    if private_path(relative):
                        continue
                    if searched >= 2000:
                        break
                    searched += 1
                    try:
                        lines = path.read_bytes().decode("utf-8").splitlines(keepends=True)
                    except (OSError, UnicodeError):
                        continue
                    offset = 0
                    for number, line in enumerate(lines, 1):
                        if query in line:
                            match_offset = offset + line.index(query)
                            matches.append({"path": relative, "line": number, "offset": max(0, match_offset - 300), "text": line[max(0, line.index(query) - 40):line.index(query) + 120].rstrip()})
                            if len(matches) >= 20:
                                break
                        offset += len(line)
                    if len(matches) >= 20:
                        break
                data = {"matches": matches, "limit_reached": len(matches) == 20}
            elif name == "file_read":
                if private_path(args["relative_path"]):
                    raise ValueError("private file content is unavailable to the model")
                path = self.workspace.resolve(args["relative_path"])
                raw = path.read_bytes()
                text = raw.decode("utf-8")
                data = {"path": args["relative_path"], "sha256": digest(raw), "total_chars": len(text),
                        **self._page(text, args.get("offset", 0), args.get("limit", self.read_page_limit))}
                read_id = f"r{len(self.reads) + 1}"
                self.reads[read_id] = (path.relative_to(self.workspace.root).as_posix(), data["sha256"], data["offset"], data["text"])
                data["read_id"] = read_id
                data["fully_read"] = self._coverage(self.reads[read_id][0], data["sha256"]) >= len(text)
            elif name == "file_outline":
                data = self._file_outline(args["relative_path"], args.get("max_items", 80))
            elif name == "edit_file":
                if args["read_id"] not in self.reads:
                    raise ValueError("unknown read_id; read the target file first")
                path, sha256, _, page = self.reads[args["read_id"]]
                if args["old_text"] not in page:
                    raise ValueError("old_text was not in this read; read the relevant page")
                if args["old_text"] == args["new_text"]:
                    raise ValueError("replacement makes no change")
                data = self._patch([{"path": path, "expected_hash": sha256,
                                     "old_text": args["old_text"], "new_text": args["new_text"]}])
                data["done"] = args["done"]
            elif name == "write_file":
                data = self._write_file(args)
            elif name == "delete_file":
                data = self._delete_file(args)
            elif name == "patch_apply":
                if len(args["edits"]) > MAX_PATCH_EDITS:
                    raise ValueError("patch batch exceeds eight edits")
                if sum(len(edit["new_text"].encode("utf-8")) for edit in args["edits"]) > MAX_PATCH_REPLACEMENT_BYTES:
                    raise ValueError("patch replacement batch exceeds size limit")
                data = self._patch(args["edits"])
            elif name == "trace_symbol":
                data = self._trace_symbol(args["symbol"])
            elif name == "infra_scan":
                data = self._infra_scan()
            elif name == "web_search":
                data = await self._web_search(args["query"])
            elif name == "run_linter":
                data = await self._run_linter(args.get("relative_path"))
            elif name == "command_start":
                if not self.allow_commands:
                    raise ValueError("command execution is disabled for this workspace-locked session")
                result = await self.supervisor.run(args["command"])
                data = result
            elif name == "diff_inspect":
                changes = self.workspace.changes()
                patch = self.workspace.patch_text().encode("utf-8")
                artifact = self.artifacts.put(patch, "ion_patch") if patch else None
                data = {"changed_files": changes.changed_files, "attributable_files": changes.attributable_files,
                        "external_files": changes.external_files, "ambiguous_files": changes.ambiguous_files,
                        "patch_artifact_id": artifact.artifact_id if artifact else None,
                        "patch_bytes": len(patch), "truncated": False}
            elif name == "diff_summary":
                data = self._diff_summary(args.get("relative_paths", ()))
            elif name == "artifact_read":
                raw = self.artifacts.read(args["artifact_id"])
                decoded = raw.decode("utf-8", "replace")
                complete = self.artifacts.is_complete(args["artifact_id"])
                data = {**self._page(decoded, args.get("offset", 0), args.get("limit", 4000)),
                        "complete": complete, "lossy": not complete or decoded.encode("utf-8") != raw}
            elif name == "artifact_search":
                data = self._artifact_search(args["artifact_id"], args["query"],
                                             args.get("cursor", 0), args.get("max_matches", 20))
            else:
                data = {"summary": args["summary"]}
            artifact_id = data.get("patch_artifact_id") or data.get("artifact_id")
            result = ToolResult(operation_id=call.operation_id, status=OperationStatus.succeeded, summary=name,
                                data=data, artifact_ids=(artifact_id,) if artifact_id else (),
                                truncated=bool(data.get("truncated", False)), lossy=bool(data.get("lossy", False)))
        except ValidationError as exc:
            location = ".".join(str(part) for part in exc.path) or "arguments"
            result = self._fail(
                call,
                f"invalid {location}: {exc.validator} constraint",
                data={"reason": "invalid_arguments", "tool": name, "required": schema.get("required", [])},
            )
        except (ValueError, OSError, UnicodeError, KeyError, TypeError) as exc:
            result = self._fail(call, str(exc))
        self.results[result.operation_id] = result
        return result

    @staticmethod
    def _page(text: str, offset: int, limit: int = 4000) -> dict:
        if offset > len(text):
            raise ValueError("offset exceeds file length")
        end = min(offset + limit, len(text))
        return {"text": text[offset:end], "offset": offset, "next_offset": end if end < len(text) else None, "truncated": end < len(text)}

    def _file_outline(self, relative_path: str, max_items: int) -> dict[str, Any]:
        if private_path(relative_path):
            raise ValueError("private file content is unavailable to the model")
        path = self.workspace.resolve(relative_path)
        text = path.read_bytes().decode("utf-8")
        lines = text.splitlines()
        markers: list[dict[str, Any]] = []
        for number, line in enumerate(lines, 1):
            stripped = line.strip()
            heading = re.match(r"#{1,6}\s+(.{1,80})", stripped)
            declaration = re.match(r"(?:async\s+)?(class|def)\s+([A-Za-z_][A-Za-z_0-9]{0,79})", stripped)
            imported = re.match(r"(?:from\s+([A-Za-z_][\w.]*)\s+import|import\s+([A-Za-z_][\w.]*))", stripped)
            if heading:
                kind, label = "heading", heading.group(1)
            elif declaration:
                kind, label = declaration.group(1), declaration.group(2)
            elif imported:
                kind, label = "import", imported.group(1) or imported.group(2)
            else:
                continue
            markers.append({"kind": kind, "label": label[:80], "start_line": number})
        items = []
        for index, marker in enumerate(markers[:max_items]):
            end = markers[index + 1]["start_line"] - 1 if index + 1 < len(markers) else len(lines)
            items.append({**marker, "end_line": end})
        return {"path": path.relative_to(self.workspace.root).as_posix(), "line_count": len(lines),
                "items": items, "truncated": len(markers) > max_items}

    def _artifact_search(self, artifact_id: str, query: str, cursor: int, max_matches: int) -> dict[str, Any]:
        if not query.strip():
            raise ValueError("query must contain text")
        raw = self.artifacts.read(artifact_id)
        text = raw.decode("utf-8", "replace")
        complete = self.artifacts.is_complete(artifact_id)
        if cursor > len(text):
            raise ValueError("cursor exceeds artifact length")
        matches = []
        position = cursor
        for _ in range(max_matches):
            found = text.find(query, position)
            if found < 0:
                break
            start = max(0, found - 76)
            snippet = text[start:min(len(text), start + 160)].replace("\n", " ").replace("\r", " ")
            matches.append({"offset": found, "snippet": snippet})
            position = found + len(query)
        more = text.find(query, position) >= 0
        return {"matches": matches, "next_cursor": position if more else None,
                "truncated": more, "complete": complete, "lossy": not complete or text.encode("utf-8") != raw}

    def _diff_summary(self, relative_paths: tuple[str, ...] | list[str]) -> dict[str, Any]:
        selected = set()
        for relative in relative_paths:
            if private_path(relative):
                raise ValueError("private file content is unavailable to the model")
            path = self.workspace.resolve(relative, allow_new=True)
            selected.add(path.relative_to(self.workspace.root).as_posix())
        changes = self.workspace.changes()
        current = self.workspace._snapshot()
        paths = [path for path in changes.changed_files if (not selected or path in selected) and not private_path(path)]
        files = []
        for path in paths[:60]:
            before_size = after_size = None
            if path in self.workspace.write_contents:
                before, after = self.workspace.write_contents[path]
                before_size, after_size = len(before), len(after)
            elif path in current:
                after_size = (self.workspace.root / path).stat().st_size
            files.append({"path": path, "before_hash": self.workspace.baseline.get(path),
                          "after_hash": current.get(path), "before_bytes": before_size,
                          "after_bytes": after_size,
                          "status": ("attributable" if path in changes.attributable_files else
                                     "ambiguous" if path in changes.ambiguous_files else "external")})
        return {"files": files, "changed_count": len(paths), "truncated": len(paths) > 60}

    def _coverage(self, path: str, sha256: str, reads: dict[str, tuple[str, str, int, str]] | None = None) -> int:
        end = 0
        source = self.reads if reads is None else reads
        for offset, text in sorted((offset, text) for p, h, offset, text in source.values() if p == path and h == sha256):
            if offset > end:
                break
            end = max(end, offset + len(text))
        return end

    def _write_file(self, args: dict) -> dict:
        relative = args["relative_path"]
        if private_path(relative):
            raise ValueError("private file edits are unavailable")
        path = self.workspace.resolve(relative, allow_new=True)
        relative = path.relative_to(self.workspace.root).as_posix()
        content = args["content"].encode("utf-8")
        if path.exists():
            read_id = args.get("read_id")
            authorized = self.reads if self.visible_write_reads is None else self.visible_write_reads
            if read_id not in authorized:
                raise ValueError("existing file requires read_id; read it before rewriting")
            read_path, sha256, _, _ = authorized[read_id]
            if read_path != relative:
                raise ValueError("read_id belongs to another file")
            raw = path.read_bytes()
            if digest(raw) != sha256:
                raise ValueError("stale read; reread the changed file")
            original = raw.decode("utf-8")
            coverage = self._coverage(relative, sha256, authorized)
            if coverage < len(original):
                raise ValueError(f"read the remaining file before rewriting; next unread offset={coverage}")
            if raw == content:
                raise ValueError("replacement makes no change")
            result = self._patch([{"path": relative, "expected_hash": sha256,
                                   "old_text": original, "new_text": args["content"]}])
        else:
            if args.get("read_id"):
                raise ValueError("file was removed after reading; refusing to recreate it")
            path.parent.mkdir(parents=True, exist_ok=True)
            path = self.workspace.resolve(relative, allow_new=True)
            # Link an already written temporary file exclusively, never overwrite
            # a concurrently created target. Both paths are inside the workspace.
            fd, temp = tempfile.mkstemp(prefix=".ion-", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                os.link(temp, path)
                self.workspace.record_write(relative, None, digest(content), b"", content)
            finally:
                Path(temp).unlink(missing_ok=True)
            result = {"changed_files": [relative]}
        return {**result, "done": args["done"]}

    def _delete_file(self, args: dict) -> dict:
        relative = args["relative_path"]
        if private_path(relative):
            raise ValueError("private file edits are unavailable")
        path = self.workspace.resolve(relative)
        canonical = path.relative_to(self.workspace.root).as_posix()
        # Deletion needs only a previously observed hash, not the file body. Keep
        # read IDs valid even when context compaction omits an older body; the
        # immediate hash check below still prevents stale or external deletes.
        authorized = self.reads
        read_id = args["read_id"]
        if read_id not in authorized:
            raise ValueError("deleting an existing file requires read_id; read it before deleting")
        read_path, sha256, _, _ = authorized[read_id]
        if read_path != canonical:
            raise ValueError("read_id belongs to another file")
        raw = path.read_bytes()
        if digest(raw) != sha256:
            raise ValueError("stale read; reread the changed file")
        path.unlink()
        self.workspace.record_write(canonical, sha256, None, raw, b"")
        return {"changed_files": [canonical], "deleted_files": [canonical], "done": args["done"]}

    def _fail(self, call: ToolCall, reason: str, data: dict[str, Any] | None = None) -> ToolResult:
        result = ToolResult(operation_id=call.operation_id, status=OperationStatus.failed, summary="tool failed", data=data or {}, error=reason)
        self.results[result.operation_id] = result
        return result

    def _patch(self, edits: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(edits, list) or not edits:
            raise ValueError("edits must be a nonempty array")
        prepared = []
        seen = set()
        observed = {(path, sha256) for path, sha256, _, _ in self.reads.values()}
        for edit in edits:
            if set(edit) != {"path", "expected_hash", "old_text", "new_text"}:
                raise ValueError("invalid patch edit fields")
            relative = edit["path"]
            if private_path(relative):
                raise ValueError("private file edits are unavailable")
            path = self.workspace.resolve(relative)
            relative = path.relative_to(self.workspace.root).as_posix()
            if relative in seen:
                raise ValueError("duplicate patch path")
            seen.add(relative)
            if (relative, edit["expected_hash"]) not in observed:
                raise ValueError("patch requires an observed read of each target hash")
            raw = path.read_bytes()
            if digest(raw) != edit["expected_hash"]:
                raise ValueError("stale patch input")
            text = raw.decode("utf-8")
            old = edit["old_text"]
            if (not old and text) or text.count(old) != 1:
                raise ValueError("old_text must match exactly once")
            replacement = text.replace(old, edit["new_text"], 1).encode("utf-8")
            prepared.append((relative, path, raw, replacement))
        temp_paths = []
        try:
            for _, path, _, replacement in prepared:
                fd, name = tempfile.mkstemp(prefix=".ion-", dir=path.parent)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(replacement)
                os.chmod(name, path.stat().st_mode)
                temp_paths.append(Path(name))
            for _, path, old, _ in prepared:
                if digest(path.read_bytes()) != digest(old):
                    raise ValueError("file changed during patch")
            for (relative, path, old, replacement), temp in zip(prepared, temp_paths):
                temp.replace(path)
                self.workspace.record_write(relative, digest(old), digest(replacement), old, replacement)
        finally:
            for path in temp_paths:
                path.unlink(missing_ok=True)
        return {"changed_files": [item[0] for item in prepared]}

    def _trace_symbol(self, symbol: str) -> dict:
        from ion.tools.scanning import trace_symbol
        return trace_symbol(self.workspace, symbol)

    def _infra_scan(self) -> dict:
        from ion.tools.scanning import infra_scan
        return infra_scan(self.workspace)

    async def _web_search(self, query: str) -> dict:
        from ion.tools.research import web_search
        return await web_search(query, self.http_transport)

    async def _run_linter(self, relative_path: str | None = None) -> dict:
        if not self.allow_commands:
            raise ValueError("command execution is disabled for this session")
        from ion.tools.linting import run_linter
        return await run_linter(self.workspace, self.supervisor, self.lint_command, relative_path)
