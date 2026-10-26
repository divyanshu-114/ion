from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolAction:
    tool: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class FinishAction:
    summary: str
    evidence_ids: tuple[str, ...]


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def parse_action(text: str) -> ToolAction | FinishAction:
    data = json.loads(text, object_pairs_hook=_no_duplicates)
    if not isinstance(data, dict):
        raise ValueError("action must be an object")
    if data.get("action") == "tool" and set(data) == {"action", "tool", "arguments"}:
        if not isinstance(data["tool"], str) or not isinstance(data["arguments"], dict):
            raise ValueError("invalid tool action")
        return ToolAction(data["tool"], data["arguments"])
    if data.get("action") == "finish" and set(data) == {"action", "summary", "evidence_ids"}:
        if not isinstance(data["summary"], str) or not isinstance(data["evidence_ids"], list):
            raise ValueError("invalid finish action")
        if not all(isinstance(item, str) for item in data["evidence_ids"]):
            raise ValueError("invalid evidence ids")
        return FinishAction(data["summary"], tuple(data["evidence_ids"]))
    raise ValueError("unknown action or fields")
