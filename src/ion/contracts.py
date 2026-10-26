from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
import re


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Phase(StrEnum):
    intake = "intake"
    inspect = "inspect"
    plan = "plan"
    act = "act"
    verify = "verify"
    finalize = "finalize"


class Outcome(StrEnum):
    verified = "verified"
    unverified = "unverified"
    blocked = "blocked"
    budget_exhausted = "budget_exhausted"
    failed = "failed"
    cancelled = "cancelled"


class OperationStatus(StrEnum):
    prepared = "prepared"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    unknown = "unknown"


class ModelProfile(StrictModel):
    provider: str
    endpoint: str
    model_id: str
    api_key_env: str = "AI_API_KEY"
    protocol: Literal["openai_chat"] = "openai_chat"
    tool_protocol: Literal["native", "structured_json"] = "native"
    text_only: Literal[True] = True
    locked: bool = False
    context_window: int = Field(gt=1024)
    max_output_tokens: int = Field(gt=0)
    input_budget_tokens: int = Field(default=6000, ge=1024)

    @field_validator("api_key_env")
    @classmethod
    def valid_key_name(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*_API_KEY", value):
            raise ValueError("api_key_env must name an environment variable ending in _API_KEY")
        return value

    @model_validator(mode="after")
    def check_limits(self) -> ModelProfile:
        if self.max_output_tokens >= self.context_window:
            raise ValueError("max_output_tokens must be below context_window")
        return self


class TaskSpec(StrictModel):
    task_id: str = Field(default_factory=lambda: str(uuid4()))
    text: str
    repo_path: str
    profile_name: str
    mode: Literal["product", "evaluation"] = "product"
    criteria: tuple[str, ...] = ()

    @field_validator("text", "repo_path", "profile_name")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value cannot be blank")
        return value


class ToolCall(StrictModel):
    operation_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    tool: str
    arguments: dict[str, Any]
    status: OperationStatus = OperationStatus.prepared


class ToolResult(StrictModel):
    operation_id: str
    status: OperationStatus
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    artifact_ids: tuple[str, ...] = ()
    error: str | None = None
    duration_ms: int = 0
    truncated: bool = False
    lossy: bool = False


class ArtifactRef(StrictModel):
    artifact_id: str
    kind: str
    relative_store_path: str
    sha256: str
    byte_count: int
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    complete: bool = True
    redaction_applied: bool = False


class VerificationEvidence(StrictModel):
    kind: Literal["executable", "observation", "static"]
    command_operation_id: str | None = None
    source_operation_ids: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def check_source(self) -> VerificationEvidence:
        if self.kind == "executable" and not self.command_operation_id:
            raise ValueError("executable evidence requires a command")
        if self.kind != "executable" and not (self.source_operation_ids or self.source_refs or self.artifact_ids):
            raise ValueError("evidence requires a source")
        return self


class VerificationRecord(StrictModel):
    verification_id: str = Field(default_factory=lambda: str(uuid4()))
    criterion_ids: tuple[str, ...]
    evidence: VerificationEvidence
    workspace_fingerprint: str
    status: Literal["passed", "failed", "unavailable", "invalidated"]
    limitations: tuple[str, ...] = ()


class MemoryRecord(StrictModel):
    memory_id: str = Field(default_factory=lambda: str(uuid4()))
    scope: str
    fact_key: str
    text: str
    evidence_kind: Literal["observed", "user_asserted", "hypothesis", "derived"]
    status: Literal["active", "stale", "superseded", "expired", "forgotten"] = "active"
    source_refs: tuple[str, ...] = ()
    supporting_hashes: tuple[str, ...] = ()
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    supersedes_id: str | None = None
    extraction_version: str = "deterministic-1"

    @model_validator(mode="after")
    def check_memory(self) -> MemoryRecord:
        if not self.scope.strip() or not self.fact_key.strip() or not self.text.strip():
            raise ValueError("memory scope, fact_key, and text cannot be blank")
        if self.evidence_kind != "user_asserted" and not self.source_refs:
            raise ValueError("non-user memory requires source references")
        return self


class ContextCheckpoint(StrictModel):
    checkpoint_id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    through_seq: int = Field(ge=0)
    structured_state_version: int = Field(default=1, ge=1)
    amendment_version: int = Field(default=0, ge=0)
    constraints_digest: str
    summary: str
    recent_event_refs: tuple[str, ...] = ()
    pinned_evidence_refs: tuple[str, ...] = ()
    model_profile_digest: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ContextManifest(StrictModel):
    included_turn_ids: tuple[str, ...] = ()
    omitted_turn_ids: tuple[str, ...] = ()
    checkpoint_id: str | None = None
    pinned_evidence_refs: tuple[str, ...] = ()
    selected_tool_names: tuple[str, ...] = ()
    estimated_input_tokens: int = Field(ge=0)
    output_cap: int = Field(gt=0)
    omission_reasons: dict[str, str] = Field(default_factory=dict)


class BudgetReport(StrictModel):
    requests_used: int = Field(ge=0)
    requests_remaining: int = Field(ge=0)
    settled_tokens: int = Field(ge=0)
    reserved_tokens: int = Field(ge=0)
    token_limit: int | None = Field(default=None, ge=0)
    deadline_reached: bool
    protected_tokens: int = Field(default=0, ge=0)
    estimated_input_tokens: int | None = Field(default=None, ge=0)
    output_cap: int | None = Field(default=None, ge=0)


class TaskResult(StrictModel):
    schema_version: Literal[1] = 1
    task_id: str
    outcome: Outcome
    summary: str
    changed_files: tuple[str, ...] = ()
    patch_artifact_id: str | None = None
    verification_ids: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    final_workspace_fingerprint: str | None = None
    requests_used: int = 0
    reported_input_tokens: int = 0
    reported_output_tokens: int = 0
    accounted_tokens: int = 0
    error_category: str | None = None
    request_dispatched: bool | None = None
    budget: BudgetReport | None = None


class ModelRequest(StrictModel):
    messages: tuple[dict[str, Any], ...]
    tools: tuple[dict[str, Any], ...] = ()
    max_output_tokens: int
    profile_digest: str
    tool_choice: str | None = None


class ModelEvent(StrictModel):
    kind: Literal["text_delta", "tool_call", "usage", "completed", "error"]
    text: str = ""
    tool: str | None = None
    arguments: dict[str, Any] | None = None
    call_id: str | None = None
    usage: dict[str, int] | None = None
    error: str | None = None
    finish_reason: str | None = None
    retry_after_seconds: float | None = Field(default=None, ge=0)


class ModelInfo(StrictModel):
    model_id: str
    context_window: int | None = None
    max_output_tokens: int | None = None
    text_only: bool | None = None
    supports_tools: bool | None = None
    free: bool | None = None
    available: bool = True


class ProbeResult(StrictModel):
    model_id: str
    text_ok: bool
    tools_ok: bool
    protocol: str
    profile_digest: str


class EngineEvent(StrictModel):
    phase: Phase
    message: str
