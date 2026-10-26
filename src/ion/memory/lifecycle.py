from __future__ import annotations

from ion.memory.store import MemoryStore


def invalidate_sources(store: MemoryStore, source_refs: list[str] | tuple[str, ...]) -> int:
    return sum(store.invalidate(source) for source in dict.fromkeys(source_refs))


def forget(store: MemoryStore, memory_id: str) -> None:
    store.forget(memory_id)
