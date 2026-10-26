from __future__ import annotations

from ion.contracts import MemoryRecord
from ion.memory.store import MemoryStore


class MemoryRetriever:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def retrieve(self, scope: str, query: str, budget: int = 10) -> list[MemoryRecord]:
        return self.store.query(scope, query, budget)

    @staticmethod
    def render(records: list[MemoryRecord], max_chars: int = 1800) -> str:
        lines: list[str] = []
        size = 0
        for record in records:
            line = f"[{record.evidence_kind}] {record.fact_key}: {record.text} (sources: {', '.join(record.source_refs)})"
            if size + len(line) + 1 > max_chars:
                break
            lines.append(line)
            size += len(line) + 1
        return "\n".join(lines)
