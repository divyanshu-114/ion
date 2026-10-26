from datetime import datetime, timedelta, timezone

import pytest

from ion.memory.store import MemoryStore


def test_memory_observations_are_scoped_searchable_and_source_linked(tmp_path):
    store = MemoryStore(tmp_path / "knowledge.sqlite3")
    observed = store.observe({
        "scope": "repo:demo",
        "fact_key": "test.command",
        "text": "Run python -m pytest tests",
        "evidence_kind": "observed",
        "source_refs": ["README.md:abc"],
        "supporting_hashes": ["abc"],
    })
    assert observed.status == "active"
    assert [item.memory_id for item in store.query("repo:demo", "pytest")] == [observed.memory_id]
    assert store.query("repo:other", "pytest") == []
    store.close()


def test_invalidation_and_forgetting_remove_facts_from_active_retrieval(tmp_path):
    store = MemoryStore(tmp_path / "knowledge.sqlite3")
    first = store.observe({"scope": "repo", "fact_key": "entry", "text": "src/app.py", "evidence_kind": "observed", "source_refs": ["app.py:one"]})
    derived = store.observe({"scope": "repo", "fact_key": "recipe", "text": "Run the app tests", "evidence_kind": "derived", "source_refs": ["app.py:one"], "supporting_hashes": ["one"]})
    assert store.invalidate("app.py:one") >= 2
    assert store.query("repo", "app") == []
    replacement = store.observe({"scope": "repo", "fact_key": "entry", "text": "src/main.py", "evidence_kind": "observed", "source_refs": ["main.py:two"]})
    store.forget(replacement.memory_id)
    assert store.query("repo", "main") == []
    store.close()


def test_same_fact_records_retain_history_and_explicit_replacement(tmp_path):
    store = MemoryStore(tmp_path / "knowledge.sqlite3")
    old = store.observe({"scope": "repo", "fact_key": "test.command", "text": "pytest", "evidence_kind": "observed", "source_refs": ["pyproject.toml:a"]})
    new = store.observe({"scope": "repo", "fact_key": "test.command", "text": "pytest -q", "evidence_kind": "observed", "source_refs": ["pyproject.toml:b"], "supersedes_id": old.memory_id})
    assert store.get(old.memory_id).status == "superseded"
    assert store.get(new.memory_id).status == "active"
    with pytest.raises(ValueError, match="superseded memory"):
        store.observe({"scope": "repo", "fact_key": "orphan", "text": "orphan", "evidence_kind": "derived", "source_refs": ["missing"], "supersedes_id": "missing-id"})
    store.close()


def test_future_memory_is_not_retrieved_until_valid_from(tmp_path):
    store = MemoryStore(tmp_path / "knowledge.sqlite3")
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    record = store.observe({
        "scope": "repo",
        "fact_key": "future.command",
        "text": "Run the future check",
        "evidence_kind": "observed",
        "source_refs": ["future"],
        "valid_from": future,
    })
    assert store.query("repo", "future") == []
    assert store.profile("repo") == []
    assert store.get(record.memory_id).valid_from == future
    store.close()
