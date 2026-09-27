# Output-Aware Budgets, Progressive Tools, and Durable Chat Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix Ion's false `budget_exhausted` failures, preserve enough output capacity for complete useful actions, add bounded tools that reduce repeated context, and make chat/context resumable after restart.

**Architecture:** Keep `BudgetLedger` as the concurrency-safe accounting authority, add a pure `BudgetPolicy` for phase-specific output decisions, and make `ContextManager` return a selection manifest plus typed overflow failures. Add progressive tool bundles with bounded results, then persist chat turns, request settlements, and checkpoints in the existing per-session SQLite store so `/resume` can reconstruct context without replaying side effects.

**Tech Stack:** Python 3.12, Pydantic 2, SQLite/WAL, pytest/pytest-asyncio, Textual, existing artifact and workspace abstractions.

**Spec:** `docs/superpowers/specs/2026-09-27-output-aware-budgets-durable-chat-context-design.md`

## Global Constraints

- “Output is protected capacity. A request is admitted only when it can include required input and a phase-appropriate minimum output allowance.”
- “A rejected request records a physical attempt but zero model-token usage.”
- “The journal is authoritative. SQLite chat turns, request records, operations, and checkpoints define resumable state.”
- “Resume is conservative. Completed turns are reconstructed; unresolved operations block continuation until reconciled.”
- “The controller must not add a tool merely because it exists.”
- “The selector never silently removes system policy, original task text, effective user steering, active constraints, pending operation identities, or the latest complete tool exchange.”
- “One bounded compaction recovery is attempted at most once for a logical turn.”
- “Switching models without the user's explicit selection” is out of scope.
- “Replaying filesystem edits, commands, or other tool side effects during resume” is out of scope.
- Preserve owner-only storage, path guards, workspace ownership, verification evidence rules, and the existing provider error taxonomy.

## Review Focus

- Latest tool turn exceeds the input budget: it must become a typed context blocker with no provider request; pinned by `test_engine_reports_context_overflow_without_dispatch` in Task 4.
- Provider rejects after admission: it must record a physical attempt with zero model-token usage; pinned by `test_http_rejection_settles_zero_usage` in Task 1 and Task 4 integration coverage.
- Provider returns incomplete or unknown usage: estimates remain distinguishable from reported usage and are never silently converted to zero; pinned by `test_unknown_usage_remains_unsettled` in Task 1.
- More tools increase schema cost: the phase bundle must stay bounded and expose artifact/outline tools only when evidence exists; pinned by Task 2 schema-size tests and Task 3 context-budget tests.
- Restart follows a partially completed side effect: resume must show the chat but deny write continuation until the operation is reconciled; pinned by `test_restore_blocks_unresolved_operations` in Task 6 and the TUI test in Task 7.

---

### Task 1: Add typed budget decisions, reservations, and output-quality reserves

**Files:**
- Create: `src/ion/budget_policy.py`
- Modify: `src/ion/budget.py:1-70`
- Modify: `src/ion/contracts.py:16-75,180-214`
- Modify: `tests/test_budgets.py:1-24`
- Create: `tests/test_budget_policy.py`

**Interfaces:**
- Consumes: `Phase`, `ModelProfile`, and existing `BudgetLedger` counters.
- Produces: `WorkClass`, `OutputPlan`, `BudgetSnapshot`, `BudgetFailureCode`, `BudgetError`, and `BudgetLedger.snapshot()` for Tasks 3–7.

- [ ] **Step 1: Write failing tests for the output policy and typed failures.**

  Add `test_small_action_gets_reduced_cap_when_profile_max_would_not_fit`, asserting that a small edit with enough room for 384 output is admitted with a cap no larger than 1,024 even when the profile maximum is larger; add `test_minimum_output_failure_is_typed`, asserting `BudgetError.code == "token_limit"`, `dispatched is False`, and the details contain input, minimum output, and remaining tokens; add `test_unknown_usage_remains_unsettled`, asserting a reservation remains marked estimated when no complete provider usage is supplied.

- [ ] **Step 2: Run the focused tests to verify they fail.**

  Run: `pytest tests/test_budgets.py tests/test_budget_policy.py -q`

  Expected: FAIL because `BudgetPolicy`, typed failure metadata, and ledger snapshots do not exist.

- [ ] **Step 3: Implement `BudgetPolicy.plan(work_class: WorkClass, input_tokens: int, profile: ModelProfile, snapshot: BudgetSnapshot) -> OutputPlan` in `src/ion/budget_policy.py`.**

  Encode the spec tiers exactly: inspect 256/512, edit 384/1,024, rewrite 768/profile maximum, verify 256/512, finalize 256/512. Select the largest cap that fits after the protected verification/finalization reserve; raise `BudgetError` when the minimum cannot fit. Keep this class pure so it can be tested without a provider.

- [ ] **Step 4: Extend `BudgetLedger` with `snapshot() -> BudgetSnapshot`, protected-token admission, and typed settlement state.**

  Preserve the existing `reserve`/`settle` behavior and concurrency lock. Add `protected_tokens` to the admission calculation, retain the estimate until complete usage is reported, and expose requests used/remaining, settled tokens, reserved tokens, token limit, and deadline state. A rejected provider request must be settleable with `model_tokens=0` without erasing the fact that a physical attempt occurred.

- [ ] **Step 5: Add contract fields for quality-preserving task results.**

  Add a strict `BudgetReport` model and optional `TaskResult.error_category`, `TaskResult.request_dispatched`, and `TaskResult.budget` fields with backward-compatible defaults. Keep `Outcome.budget_exhausted` for request/token/deadline exhaustion; prompt-fit errors will use `blocked` plus `context_overflow` in a later engine task.

- [ ] **Step 6: Run the focused tests and commit.**

  Run: `pytest tests/test_budgets.py tests/test_budget_policy.py -q`

  Expected: PASS.

  Commit: `git add src/ion/budget.py src/ion/budget_policy.py src/ion/contracts.py tests/test_budgets.py tests/test_budget_policy.py && git commit -m "feat: add output-aware budget decisions"`

### Task 2: Add bounded progressive tool capabilities

**Files:**
- Create: `src/ion/tools/bundles.py`
- Modify: `src/ion/tools/registry.py:18-109,143-246`
- Modify: `src/ion/artifacts.py:11-40`
- Modify: `tests/test_low_token.py:18-59,118-125`
- Create: `tests/test_tool_bundles.py`

**Interfaces:**
- Consumes: `Phase`, `ToolDispatcher`, `ArtifactStore`, workspace hashes, and observed evidence.
- Produces: `select_tool_bundle(...) -> tuple[str, ...]`, bounded `file_outline`, `artifact_search`, `artifact_read`, `diff_summary`, and guarded `patch_apply` behavior for Tasks 3–4.

- [ ] **Step 1: Write failing tests for tool behavior, bounds, and progressive exposure.**

  Add tests asserting `file_outline` returns ranges without source bodies, `artifact_search` returns at most `max_matches` bounded snippets with a cursor, `artifact_read` respects offset/limit and reports lossiness, `diff_summary` excludes the full patch body, and `patch_apply` rejects more than eight edits or replacements exceeding the configured batch limit. Add a schema test asserting Navigate and Verify bundles are smaller than the complete registry and that `artifact_search` is absent until an artifact exists.

- [ ] **Step 2: Run the focused tool tests to verify they fail.**

  Run: `pytest tests/test_tool_bundles.py tests/test_low_token.py -q`

  Expected: FAIL because the new tool handlers and bundle selector do not exist.

- [ ] **Step 3: Implement bounded read-only handlers in `ToolDispatcher`.**

  Implement `file_outline(relative_path: str, max_items: int = 80) -> ToolResult` using deterministic line/range extraction; do not add a parser dependency. Implement `artifact_search(artifact_id: str, query: str, cursor: int = 0, max_matches: int = 20) -> ToolResult` over `ArtifactStore.read()` with bounded snippets and `next_cursor`. Extend `artifact_read` with explicit bounded `limit` validation and preserve `truncated`/`lossy` flags. Implement `diff_summary(relative_paths: tuple[str, ...] = ()) -> ToolResult` from workspace change metadata without serializing the whole patch.

- [ ] **Step 4: Promote guarded `patch_apply` and define `select_tool_bundle(...)`.**

  Keep exact path/hash validation and the existing workspace write protections. Enforce a maximum of eight edits and a bounded aggregate replacement size before calling `_patch`. In `src/ion/tools/bundles.py`, select Navigate, Edit, Verify, or Recover names from phase, `edit_intent`, `observed_page_count`, artifact availability, and `allow_commands`; include `edit_file` only after a read, `patch_apply` only when target hashes are available, and artifact tools only when artifacts exist.

- [ ] **Step 5: Run the focused tests and commit.**

  Run: `pytest tests/test_tool_bundles.py tests/test_low_token.py -q`

  Expected: PASS, including existing economy workflow tests.

  Commit: `git add src/ion/tools/bundles.py src/ion/tools/registry.py src/ion/artifacts.py tests/test_tool_bundles.py tests/test_low_token.py && git commit -m "feat: add bounded progressive tool bundles"`

### Task 3: Make context selection output-aware and typed

**Files:**
- Modify: `src/ion/context.py:1-120`
- Modify: `src/ion/contracts.py:166-178`
- Modify: `tests/test_context.py:1-22`
- Create: `tests/test_context_quality.py`

**Interfaces:**
- Consumes: `OutputPlan`, selected tool schemas, checkpoint metadata, artifacts, and existing history.
- Produces: `ContextManifest`, `ContextOverflowError`, and `ContextManager.build(..., input_budget_tokens=..., tool_names=..., checkpoint=...) -> ContextPacket` with manifest data for Tasks 4–6.

- [ ] **Step 1: Write failing tests for optional-first trimming and complete latest actions.**

  Add `test_optional_memory_and_duplicate_reads_drop_before_required_context`, asserting task text, steering, constraints, and the latest complete tool turn remain while optional memory and duplicate read bodies are removed. Add `test_required_latest_turn_raises_context_overflow`, asserting a typed `ContextOverflowError` with `dispatched=False` rather than a generic `ValueError`. Add `test_bundle_schema_cost_is_included_in_estimate`, asserting the same history with Verify tools has a larger estimate than Navigate tools and still respects the selected input allowance.

- [ ] **Step 2: Run context tests to verify they fail.**

  Run: `pytest tests/test_context.py tests/test_context_quality.py -q`

  Expected: FAIL because `ContextManifest`, typed overflow, and tool-specific estimates do not exist.

- [ ] **Step 3: Add `ContextManifest` and typed overflow handling.**

  Record included/omitted turn identifiers, checkpoint ID, pinned evidence, selected tool names, estimated input tokens, output cap, and omission reasons. Replace string-matched `ValueError` cases with `ContextOverflowError(code, message, manifest)` while preserving the existing valid-message/tool-call pairing guarantees.

- [ ] **Step 4: Update `ContextManager.build(...)` to accept the selected input allowance and tool names.**

  Compute the limit from the chosen output cap and context headroom. Trim in the spec order: repository memory, duplicate/stale read bodies, verbose artifact previews, checkpoint-covered old turns, then old completed turns. Never remove policy, task, steering, active constraints, pending operations, or the latest complete tool exchange. Return the manifest with the packet.

- [ ] **Step 5: Run the focused tests and commit.**

  Run: `pytest tests/test_context.py tests/test_context_quality.py -q`

  Expected: PASS.

  Commit: `git add src/ion/context.py src/ion/contracts.py tests/test_context.py tests/test_context_quality.py && git commit -m "feat: make context selection output-aware"`

### Task 4: Integrate budget decisions, bundles, and correct failure reporting in the engine

**Files:**
- Modify: `src/ion/engine.py:23-547`
- Modify: `src/ion/config.py:14-29`
- Modify: `ion.toml`
- Modify: `tests/test_engine.py:1-180`
- Modify: `tests/test_low_token.py:23-59,127-147`
- Create: `tests/test_budget_engine.py`

**Interfaces:**
- Consumes: `BudgetPolicy`, `BudgetLedger`, `ContextManifest`, `ContextOverflowError`, and `select_tool_bundle`.
- Produces: engine runs that expose `BudgetReport`, typed stop reasons, request-dispatch state, and quality-preserving output caps for Tasks 5–7.

- [ ] **Step 1: Write the regression test for the reported bug.**

  Add `test_engine_reports_context_overflow_without_dispatch`: configure a small profile, create a latest tool result too large to fit even after optional trimming, run a task with a scripted provider, and assert `len(provider.requests) == 0`, `result.outcome == "blocked"`, `result.error_category == "context_overflow"`, `result.request_dispatched is False`, and the summary says no model request was sent. Add `test_small_final_action_uses_dynamic_output_cap` and assert a short final action is dispatched after a large settled input where the old full-cap reservation would have stopped.

- [ ] **Step 2: Run the regression tests to verify they fail.**

  Run: `pytest tests/test_budget_engine.py tests/test_engine.py -q`

  Expected: FAIL because the engine currently maps the context exception to budget exhaustion and always reserves its selected full cap without a typed report.

- [ ] **Step 3: Integrate progressive bundles and output planning into the run loop.**

  Select the work class from phase, edit intent, current tool/evidence state, and rewrite intent. Select tool names before building context; include schema cost in the estimate. Ask `BudgetPolicy.plan(...)`, then call the ledger admission with the selected cap and protected reserve. Retry admission only after refreshing the ledger snapshot; never dispatch when admission fails.

- [ ] **Step 4: Replace string-based exception mapping with typed result reporting.**

  Catch `BudgetError` and `ContextOverflowError` explicitly. Set `blocked/context_overflow` for prompt-fit failures, `budget_exhausted` for hard request/token/deadline exhaustion, and preserve existing provider quota/rate-limit outcomes. Populate `TaskResult.budget`, `error_category`, and `request_dispatched`. Emit concise events containing current cap, estimate, reservation, settled usage, and remaining reserve.

- [ ] **Step 5: Preserve output quality across retries and completion.**

  Keep one malformed-action/tool-failure repair, one output-truncation recovery, and one compaction overflow recovery. Do not execute partial tool calls. Do not mark an edit verified without executable/static evidence accepted by `CompletionGate`. Keep the existing honest `unverified` result when checks are unavailable.

- [ ] **Step 6: Add economy configuration fields and run focused integration tests.**

  Add validated defaults for output tiers, verification/finalization reserves, tool preview maximum, and one compaction recovery. Preserve current profiles and backward-compatible defaults in `ion.toml`/`AppConfig`.

  Run: `pytest tests/test_budget_engine.py tests/test_engine.py tests/test_low_token.py -q`

  Expected: PASS, including the original three-request economy edit flow and the new bug regression.

  Commit: `git add src/ion/engine.py src/ion/config.py ion.toml tests/test_engine.py tests/test_low_token.py tests/test_budget_engine.py && git commit -m "fix: classify context overflow and preserve output budget"`

### Task 5: Persist chat turns, request settlements, epochs, and checkpoints in SQLite

**Files:**
- Modify: `src/ion/contracts.py:70-84,166-214`
- Modify: `src/ion/storage.py:16-208`
- Modify: `src/ion/compaction.py:12-93`
- Modify: `tests/test_storage.py:1-54`
- Modify: `tests/test_compaction.py:1-30`
- Create: `tests/test_chat_persistence.py`

**Interfaces:**
- Consumes: `ChatTurn`, `BudgetReport`, `ContextManifest`, `ContextCheckpoint`, `EngineEvent`, and artifact references.
- Produces: `RunStore.append_chat_turn(...)`, `chat_turns(...)`, `begin_model_request(...)`, `settle_model_request(...)`, `commit_context_epoch(...)`, `latest_context_epoch(...)`, and transactional schema migration for Task 6.

- [ ] **Step 1: Write failing persistence and migration tests.**

  Add tests that close/reopen the database and preserve ordered user, assistant, and tool turns; persist a request before dispatch and settle it with reported usage or zero usage; preserve estimated/unknown usage distinctly; atomically commit a checkpoint with `compaction.completed`; leave the prior checkpoint intact when validation fails; and migrate an existing v1 run database without losing runs, events, operations, or deduplication rows.

- [ ] **Step 2: Run storage tests to verify they fail.**

  Run: `pytest tests/test_storage.py tests/test_compaction.py tests/test_chat_persistence.py -q`

  Expected: FAIL because the new tables and store methods do not exist.

- [ ] **Step 3: Add strict persistence records and SQLite migrations.**

  Add models for chat turns, model requests, context epochs, and budget reports. Add optional backward-compatible `TaskSpec.parent_task_id` so Fork can link to its source session. Add transactional `PRAGMA user_version` migration with a recoverable sibling backup and read-only failure behavior for newer versions. Keep owner-only permissions and WAL/foreign-key settings.

- [ ] **Step 4: Implement append/settle methods with idempotent sequence handling.**

  Persist normalized JSON content, SHA-256 digest, role/kind/visibility, operation/artifact references, request status, provider usage, and context manifests. Ensure a request record exists before dispatch and a duplicate settlement cannot overwrite a different attempt. Keep full side-effect results in the existing operations/artifacts path.

- [ ] **Step 5: Move new checkpoint commits behind `RunStore.commit_context_epoch(...)`.**

  Validate summary sections, amendment version, constraints digest, and referenced sequences before committing the checkpoint and `compaction.completed` event in one transaction. Retain the existing JSON checkpoint reader for migration, but write new checkpoints to SQLite.

- [ ] **Step 6: Run tests and commit.**

  Run: `pytest tests/test_storage.py tests/test_compaction.py tests/test_chat_persistence.py -q`

  Expected: PASS, including existing operation recovery tests.

  Commit: `git add src/ion/contracts.py src/ion/storage.py src/ion/compaction.py tests/test_storage.py tests/test_compaction.py tests/test_chat_persistence.py && git commit -m "feat: persist chat turns and context epochs"`

### Task 6: Wire durable turns into the engine and implement safe context restoration

**Files:**
- Modify: `src/ion/engine.py:23-547`
- Modify: `src/ion/session.py:16-78`
- Modify: `src/ion/storage.py:178-205`
- Create: `src/ion/session_context.py`
- Modify: `tests/test_session_events.py:1-28`
- Create: `tests/test_session_context.py`
- Create: `tests/test_engine_persistence.py`

**Interfaces:**
- Consumes: Task 5 store methods and `ContextManager` manifests.
- Produces: `RestoredContext`, `SessionService.restore(session_id) -> RestoredContext`, and `Engine.run(task, restored_context: RestoredContext | None = None)` with durable turn/request/checkpoint writes.

- [ ] **Step 1: Write failing restore tests.**

  Add `test_restore_rebuilds_ordered_history_after_reopen`, asserting the exact user/assistant/tool order and latest committed checkpoint are returned; add `test_restore_blocks_unresolved_operations`, asserting the result contains the unresolved operation and cannot grant write continuation; add `test_resume_does_not_dispatch`, asserting restoration alone creates zero provider requests.

- [ ] **Step 2: Run restore tests to verify they fail.**

  Run: `pytest tests/test_session_context.py tests/test_engine_persistence.py -q`

  Expected: FAIL because `RestoredContext` and session restoration do not exist.

- [ ] **Step 3: Implement `RestoredContext` and `SessionService.restore(...)`.**

  Load the task, ordered chat turns, latest epoch/checkpoint, budget report, final result, unresolved operations, and stale evidence markers. Recheck workspace identity and current hashes; mark changed read evidence stale. Return a read-only restoration object and a separate explicit continuation decision.

- [ ] **Step 4: Persist engine turns and request lifecycle at safe boundaries.**

  Append task/steering turns before acknowledgment, request/context-epoch records before provider dispatch, validated assistant turns after complete responses, and tool turns only after operation settlement. Persist compaction events/checkpoints through the store. Do not persist partial provider streams as completed assistant turns.

- [ ] **Step 5: Add restored history as an engine input without replaying effects.**

  Seed `history` from `RestoredContext`, inject current effective constraints independently of the checkpoint, and require stale files to be reread before edits. Never call the provider or dispatcher during `restore`; only `Engine.run` after an explicit Continue may dispatch.

- [ ] **Step 6: Run tests and commit.**

  Run: `pytest tests/test_session_events.py tests/test_session_context.py tests/test_engine_persistence.py -q`

  Expected: PASS, with unresolved operation blocking preserved.

  Commit: `git add src/ion/engine.py src/ion/session.py src/ion/storage.py src/ion/session_context.py tests/test_session_events.py tests/test_session_context.py tests/test_engine_persistence.py && git commit -m "feat: restore durable chat context safely"`

### Task 7: Expose budget details and Continue/Fork resume behavior in the TUI

**Files:**
- Modify: `src/ion/tui/app.py:90-105,145-180,253-305,352-560`
- Modify: `src/ion/tui/widgets.py`
- Modify: `src/ion/tui/app.tcss:166-231`
- Modify: `tests/test_tui.py:69-122`
- Create: `tests/test_tui_budget_resume.py`

**Interfaces:**
- Consumes: `BudgetReport`, `RestoredContext`, `SessionService.restore`, and typed `TaskResult` stop reasons.
- Produces: `/budget`, restored transcript rendering, explicit Continue/Fork choices, and actionable budget/context messages.

- [ ] **Step 1: Write failing TUI tests.**

  Add a test that renders requests, input/output usage, reserved tokens, remaining tokens, protected reserve, current output cap, and checkpoint sequence in the sidebar; add a test that `/resume TASK_ID` restores the transcript without starting a provider; add a test that unresolved operations show recovery required and hide Continue; add a test that a context overflow displays “No model request was sent” and next actions.

- [ ] **Step 2: Run TUI tests to verify they fail.**

  Run: `pytest tests/test_tui.py tests/test_tui_budget_resume.py -q`

  Expected: FAIL because the sidebar currently shows one usage string and `/resume` only repopulates the composer.

- [ ] **Step 3: Add compact budget rendering and `/budget`.**

  Implement a pure `format_budget_report(report: BudgetReport) -> str` in `src/ion/tui/widgets.py`. Update `#context-info` from engine events and expose `/budget` as a detailed per-request picker/log view without putting full request payloads into the main transcript.

- [ ] **Step 4: Implement explicit restore actions.**

  Make `/resume TASK_ID` call `SessionService.restore`, render persisted chat turns and checkpoint metadata, and present `Continue`, `Fork`, or `Inspect` choices. Continue must reacquire workspace ownership and call the engine only after user selection; Fork must create a new `TaskSpec` with `parent_task_id` set to the source task and must not replay operations.

- [ ] **Step 5: Replace technical stop text with actionable explanations.**

  Render typed stop reasons with dispatched/not-dispatched state, input estimate, output cap, remaining budget, omissions, and safe next actions. Keep provider quota/rate-limit/authentication guidance intact and preserve narrow-terminal layout behavior.

- [ ] **Step 6: Run tests and commit.**

  Run: `pytest tests/test_tui.py tests/test_tui_budget_resume.py -q`

  Expected: PASS at both existing terminal sizes, with no provider request on inspection-only resume.

  Commit: `git add src/ion/tui/app.py src/ion/tui/widgets.py src/ion/tui/app.tcss tests/test_tui.py tests/test_tui_budget_resume.py && git commit -m "feat: make budget and resume state actionable in the TUI"`

### Task 8: Align runtime documentation and run the full regression/evaluation suite

**Files:**
- Modify: `docs/context-management.md`
- Modify: `docs/interfaces-and-data.md`
- Modify: `docs/execution-and-tools.md`
- Modify: `docs/task-lifecycle-and-verification.md`
- Modify: `README.md` if command/help text is stale
- Modify: `tests/test_evals.py` or create `tests/test_budget_context_acceptance.py`

**Interfaces:**
- Consumes: all runtime behavior from Tasks 1–7.
- Produces: synchronized operator documentation and an acceptance matrix for the reported bug, output quality, progressive tools, and safe resume.

- [ ] **Step 1: Add acceptance tests for the complete flow.**

  Cover the reported prompt-overflow regression, dynamic short-output admission, provider rejection with zero usage, one-and-only-one compaction repair, bounded tool schema growth, artifact search pagination, complete edit plus verification evidence, restart/resume, steering after checkpoint, and unresolved operation blocking.

- [ ] **Step 2: Run the acceptance tests to establish any remaining failures.**

  Run: `pytest tests/test_budget_context_acceptance.py -q` (or the updated evaluation test path).

  Expected: PASS after all previous tasks; failures must identify an implementation gap rather than be waived as budget limitations.

- [ ] **Step 3: Update runtime documentation.**

  Document typed budget outcomes, output tiers, progressive bundles, artifact-backed results, durable chat/checkpoint semantics, migration behavior, `/budget`, and Continue/Fork resume. Keep numerical defaults synchronized with `ion.toml` and the spec.

- [ ] **Step 4: Run the complete verification suite.**

  Run: `pytest -q`

  Expected: all tests pass, including existing provider, workspace, verification, recovery, memory, TUI, and economy tests. Also run the repository’s existing smoke/economy command if available and record its result in the final handoff.

- [ ] **Step 5: Inspect the final diff and commit.**

  Run: `git diff --check` and inspect the final patch for accidental credentials, unbounded prompt/tool payloads, claims of verification without evidence, and any change that automatically switches models or replays effects.

  Commit: `git add docs tests && git commit -m "docs: document durable budget and context behavior"`

## Handoff criteria

The bug is solved only when the regression test proves that a required-context overflow produces a typed actionable blocker with zero provider requests, and the full suite proves that small useful output can still be admitted after large input. The feature is complete only when the TUI can inspect and explicitly continue/fork a restored conversation while unresolved operations remain blocked.
