import pytest

from ion.workers import SnapshotStore, WorkerCoordinator, WorkerPolicyError


def test_worker_snapshot_is_immutable_and_bounded(tmp_path):
    (tmp_path / "a.py").write_text("value = 1\n")
    store = SnapshotStore(tmp_path / "snapshots")
    snapshot = store.capture(tmp_path, ["a.py"], quota_bytes=1000)
    (tmp_path / "a.py").write_text("value = 2\n")
    assert store.read(snapshot.snapshot_id, "a.py") == b"value = 1\n"


def test_delegation_is_opt_in_and_read_only(tmp_path):
    (tmp_path / "a.py").write_text("value = 1\n")
    store = SnapshotStore(tmp_path / "snapshots")
    snapshot = store.capture(tmp_path, ["a.py"])
    coordinator = WorkerCoordinator(store, enabled=False)
    with pytest.raises(WorkerPolicyError):
        coordinator.delegate("inspect value", snapshot.snapshot_id, request_budget=2)
    enabled = WorkerCoordinator(store, enabled=True)
    result = enabled.delegate("inspect value", snapshot.snapshot_id, request_budget=2)
    assert result.snapshot_id == snapshot.snapshot_id
    with pytest.raises(WorkerPolicyError):
        enabled.validate_tool("command_start")
