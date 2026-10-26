import json
import os

import pytest

from ion.recovery import WorkspaceLease, WorkspaceRecoveryRequired


def test_workspace_lease_blocks_second_writer_and_releases_cleanly(tmp_path):
    registry = tmp_path / "ion" / "ownership.json"
    first = WorkspaceLease(tmp_path, registry, session_id="session-a")
    first.acquire()
    with pytest.raises(WorkspaceRecoveryRequired):
        WorkspaceLease(tmp_path, registry, session_id="session-b").acquire()
    first.release()
    payload = json.loads(registry.read_text())
    assert payload["status"] == "clean"
    second = WorkspaceLease(tmp_path, registry, session_id="session-b")
    second.acquire()
    second.release()


def test_dead_owner_remains_recovery_required_after_process_exit(tmp_path):
    registry = tmp_path / "ownership.json"
    registry.write_text(json.dumps({
        "workspace_id": WorkspaceLease.workspace_id(tmp_path),
        "owner_session_id": "dead",
        "engine_pid": 99999999,
        "status": "active",
        "pending_operation_refs": ["op-1"],
    }))
    with pytest.raises(WorkspaceRecoveryRequired, match="recovery required"):
        WorkspaceLease(tmp_path, registry, session_id="new").acquire()
    payload = json.loads(registry.read_text())
    assert payload["status"] == "recovery_required"
    assert payload["pending_operation_refs"] == ["op-1"]


def test_release_is_idempotent(tmp_path):
    lease = WorkspaceLease(tmp_path, tmp_path / "ownership.json", session_id="session")
    lease.acquire()
    lease.release()


def test_recovery_marker_is_not_cleared_by_release(tmp_path):
    registry = tmp_path / "ownership.json"
    lease = WorkspaceLease(tmp_path, registry, session_id="session")
    lease.acquire()
    lease.mark_recovery_required(["op-1"])
    lease.release()
    assert json.loads(registry.read_text())["status"] == "recovery_required"
    lease.release()


def test_workspace_lease_rejects_unknown_registry_state(tmp_path):
    registry = tmp_path / "ownership.json"
    registry.write_text(json.dumps({"status": "mystery", "workspace_id": WorkspaceLease.workspace_id(tmp_path)}))
    with pytest.raises(WorkspaceRecoveryRequired, match="unknown state"):
        WorkspaceLease(tmp_path, registry).acquire()


def test_workspace_lease_rejects_non_object_registry(tmp_path):
    registry = tmp_path / "ownership.json"
    registry.write_text("[]")
    with pytest.raises(WorkspaceRecoveryRequired, match="not an object"):
        WorkspaceLease(tmp_path, registry).acquire()
