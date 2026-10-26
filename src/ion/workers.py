from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from ion.snapshots import SnapshotStore


class WorkerPolicyError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkerHandle:
    child_id: str
    goal: str
    snapshot_id: str
    request_budget: int
    status: str = "queued"


class WorkerCoordinator:
    READ_ONLY_TOOLS = {"repo_list", "repo_search", "file_read", "artifact_read"}

    def __init__(self, snapshots: SnapshotStore, *, enabled: bool = False, max_requests: int = 10) -> None:
        self.snapshots = snapshots
        self.enabled = enabled
        self.max_requests = max_requests

    def delegate(self, goal: str, snapshot_id: str, request_budget: int) -> WorkerHandle:
        if not self.enabled:
            raise WorkerPolicyError("delegation is disabled until its benefit gate passes")
        if not goal.strip():
            raise ValueError("worker goal cannot be blank")
        if request_budget < 1 or request_budget > self.max_requests:
            raise WorkerPolicyError("worker request budget exceeds policy")
        # Validate the snapshot exists before returning a handle.
        self.snapshots.read(snapshot_id, "manifest.json")
        return WorkerHandle(str(uuid4()), goal.strip(), snapshot_id, request_budget)

    def validate_tool(self, tool: str) -> None:
        if tool not in self.READ_ONLY_TOOLS:
            raise WorkerPolicyError(f"worker tool is read-only; {tool} is unavailable")
