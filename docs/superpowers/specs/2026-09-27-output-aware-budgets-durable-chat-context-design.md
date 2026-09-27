# Output-Aware Budgets and Durable Chat Context Design

**Status:** Proposed

**Date:** 2026-09-27

## Purpose

Ion should spend a bounded token budget on useful task progress while preserving enough output capacity to produce complete tool actions, verification decisions, and final reports. When a run stops, it should distinguish provider limits, prompt-fit failures, request limits, and total-token exhaustion instead of presenting all of them as `budget_exhausted`.

Ion should also persist the conversation state needed to inspect and safely continue a task after the TUI or process restarts. Resuming restores chat and model context, but never replays tool side effects automatically.

## Current behavior and problem

`ContextManager.build()` assembles a prompt and removes older complete tool turns until the request fits. It raises a `ValueError` when required context or the latest tool turn still exceeds the prompt budget. `Engine.run()` currently maps exceptions containing `budget` to `budget_exhausted`, which makes prompt-fit failures look like consumption failures even when no model request was sent.

`BudgetLedger.admit()` reserves estimated input plus the full requested output cap. Actual reported usage later replaces that reservation, but the initial reservation can reject useful work that would have produced a short response. The ledger also exposes only aggregate counters, so the TUI cannot explain which reserve or limit stopped the run.

`RunStore` already persists task records, ordered engine events, operations, deduplicated control requests, and final results. It does not persist normalized chat turns, context-selection manifests, request reservations, or committed context epochs. `/resume` therefore restores the original task text for re-submission rather than reconstructing the prior conversation.

## Goals

- Preserve a minimum viable output allowance before sending each model request.
- Allocate larger output only to phases and actions that need it.
- Settle reservations to provider-reported usage without charging rejected requests token usage.
- Classify prompt fit, token budget, request reserve, provider quota, and provider rate-limit failures separately.
- Keep required task text, current user steering, policy, and authoritative constraints in every context epoch.
- Reduce optional and duplicate context before rejecting a turn.
- Provide more specialized, bounded tools that reduce repeated reads and oversized tool results without sending every tool schema on every turn.
- Persist user, assistant, and tool turns plus the manifests needed to reconstruct model context.
- Resume from the latest committed context epoch without automatically repeating model calls or tool operations.
- Give the TUI an actionable budget explanation and recovery choices.

## Non-goals

- Automatically increasing a configured hard token or request limit.
- Switching models without the user's explicit selection.
- Replaying filesystem edits, commands, or other tool side effects during resume.
- Persisting provider-private reasoning or encrypted continuation fields as visible chat.
- Introducing a remote conversation service or cross-device synchronization.
- Replacing Ion's repository memory database with chat history.

## Design principles

1. **Output is protected capacity.** A request is admitted only when it can include required input and a phase-appropriate minimum output allowance.
2. **Actual usage wins.** Reservations prevent oversubscription; complete provider usage replaces estimates. A rejected request records a physical attempt but zero model-token usage.
3. **The journal is authoritative.** SQLite chat turns, request records, operations, and checkpoints define resumable state. The rendered TUI transcript is a projection.
4. **Context is selected, not copied wholesale.** A context epoch records what was included and omitted. Full evidence stays in events and artifacts.
5. **Resume is conservative.** Completed turns are reconstructed; unresolved operations block continuation until reconciled.

## Architecture

The change introduces four focused units while retaining the existing engine, context manager, and run store boundaries.

### Budget policy and ledger

`BudgetPolicy` converts phase, task shape, provider limits, and remaining task budget into a `BudgetDecision`. It owns output tiers and protected reserves. `BudgetLedger` remains the concurrency-safe accounting authority and owns reservation and settlement state.

The engine asks the policy for a decision after context estimation and before constructing `ModelRequest`. The decision contains:

- estimated input tokens
- minimum output tokens
- selected output cap
- protected finalization tokens
- reserved total tokens
- remaining tokens before and after admission
- request attempt and request reserve state

Initial output tiers are configuration defaults, capped by the selected model profile:

| Work class | Minimum output | Preferred cap |
| --- | ---: | ---: |
| Inspect/search action | 256 | 512 |
| Small edit/tool action | 384 | 1,024 |
| Whole-file rewrite | 768 | profile maximum |
| Verification decision | 256 | 512 |
| Final report | 256 | 512 |

The selected cap is the largest value up to the preferred cap that fits after required input and protected finalization capacity. If the minimum output cannot fit, admission fails before dispatch with a typed reason.

The token reserve protects one final reporting response and, when executable verification remains possible, one verification decision. Deterministic diff capture, artifact creation, and result serialization remain available after model-token exhaustion.

### Typed budget and context failures

Internal budget operations return domain-specific failures instead of matching exception strings:

- `request_limit`: no physical request slots remain
- `verification_reserve`: discretionary work reached the protected request reserve
- `token_limit`: settled and reserved usage leaves insufficient total tokens
- `context_overflow`: required model input cannot fit the model/profile input limit
- `latest_turn_overflow`: the newest complete tool exchange cannot fit after optional reductions
- `deadline`: task wall-clock time expired

Each failure records whether a provider request was dispatched, the relevant limits, remaining capacity, and a suggested action. `Outcome.budget_exhausted` is reserved for hard request, token, or deadline exhaustion. Prompt-fit failures become `blocked` with error category `context_overflow` unless a bounded recovery succeeds.

### Context selection and output headroom

`ContextManager` receives a target input allowance derived from the selected output tier rather than always calculating against the profile maximum. It returns a `ContextPacket` with messages and a `ContextManifest`.

The selector removes content in this order:

1. optional repository memory and recalled navigation hints
2. duplicate or stale file-read bodies already represented by a newer read
3. verbose command output represented by an artifact reference and bounded preview
4. older completed assistant/tool exchanges already represented by a checkpoint
5. older completed exchanges not pinned by active work or verification

It never silently removes system policy, original task text, effective user steering, active constraints, pending operation identities, or the latest complete tool exchange.

If the packet still does not fit, the engine may perform one bounded checkpoint/compaction recovery before any new tool side effect. A second failure produces an actionable blocker. The recovery attempt is a physical request and participates in request and token accounting.

### Progressive tool bundles

More tools improve token efficiency only when each tool has a narrow purpose, bounded output, and a phase-specific admission rule. Ion therefore sends tool schemas progressively instead of exposing the entire registry on every request. The controller selects a bundle from the current phase, observed evidence, task intent, and remaining output budget. Tool schemas themselves are included in the context estimate.

The baseline bundles are:

| Bundle | Tools | Purpose |
| --- | --- | --- |
| Navigate | `repo_list`, `repo_search`, `file_outline`, `file_read`, `finish_request` | Find the smallest relevant source region before reading bodies. |
| Edit | `file_read`, `file_outline`, `edit_file`, `write_file`, `patch_apply`, `diff_summary`, `finish_request` | Apply one or several exact, hash-guarded edits with compact review. |
| Verify | `diff_summary`, `diff_inspect`, `command_start`, `artifact_search`, `artifact_read`, `finish_request` | Check changes and query retained output without replaying large artifacts. |
| Recover | `file_read`, `artifact_search`, `artifact_read`, `diff_summary`, `finish_request` | Reconstruct missing evidence after restart or compaction. |

The following bounded tools are added or promoted as first-class capabilities:

- `file_outline(relative_path, max_items)`: deterministic line/range metadata for imports, headings, classes, functions, and other recognizable sections. It returns no large source body.
- `artifact_search(artifact_id, query, cursor, max_matches)`: searches retained command or output artifacts and returns bounded matching snippets plus cursors. It never injects the full artifact.
- `artifact_read(artifact_id, offset, limit)`: reads a bounded artifact page and reports the next offset and whether the page is lossy.
- `diff_summary(relative_paths)`: reports changed paths, before/after hashes, and compact size/statistics without returning the complete patch.
- `patch_apply(edits)`: applies a bounded batch of exact hash-guarded replacements. Each edit must cite a read hash; the batch has a maximum edit count and replacement size and is unavailable until the required evidence is present.

`repo_list`, `repo_search`, `file_read`, `edit_file`, `write_file`, `diff_inspect`, `command_start`, and `finish_request` retain their current semantics, but their schemas and result limits become explicit bundle metadata. `artifact_read` is exposed after a command produces an artifact, not on initial navigation turns.

Tool results use a common compact envelope: status, one-line summary, bounded data, artifact IDs, truncation/lossiness flags, and a next cursor or next action when applicable. Full command output and full patches remain in artifacts. A tool that cannot satisfy its output bound returns a structured failure or artifact reference instead of flooding the next prompt.

The controller must not add a tool merely because it exists. It may add a bundle after evidence changes—for example, expose `edit_file` only after the target was read, `patch_apply` only after all target hashes are available, and `artifact_search` only after an artifact exists. Invalid or unavailable tool requests remain bounded recovery events and do not trigger a broad registry retry.

### Durable chat turns

`RunStore` gains append-only `chat_turns` records. Each turn contains:

- `task_id`
- monotonically increasing `sequence`
- `role`: `user`, `assistant`, or `tool`
- `kind`: task, steering, assistant_text, tool_calls, or tool_result
- normalized JSON content
- visibility: user-visible or context-only
- optional operation and artifact references
- SHA-256 content digest
- creation timestamp

Large tool results are stored as bounded previews with artifact references. The existing operation table remains authoritative for side-effect status and full structured tool results. Provider-private reasoning is excluded.

The user task is the first chat turn. Steering is appended durably before acknowledgment. Assistant decisions are appended after a complete provider response is validated. Tool results are appended only after operation settlement. An interrupted provider stream does not create a completed assistant turn.

### Requests, usage, and context epochs

`RunStore` gains `model_requests` and `context_epochs` records.

A model request records its logical turn ID, physical attempt, phase, profile digest, context epoch, input estimate, output cap, reservation, dispatch status, provider usage, finish reason, and typed error. The record is inserted before dispatch and settled after the stream completes or fails.

A context epoch records:

- the last included chat sequence
- checkpoint ID, when used
- included and omitted turn sequences
- pinned evidence and artifact references
- amendment/steering version
- estimated input tokens
- selected output cap
- profile digest

The epoch stores a selection manifest, not a second unbounded copy of every message. Messages are reconstructed from chat turns, checkpoints, operation results, and artifacts.

### Committed checkpoints

Context checkpoints move under the session store contract. A checkpoint and its `compaction.completed` event commit in one SQLite transaction. The previous checkpoint remains authoritative if validation or commit fails.

Each checkpoint preserves the original task, effective constraints, completed work, active work, blockers, next actions, recent event references, and pinned evidence references. It carries the chat sequence and amendment version it summarizes. Later steering is always injected independently and cannot be overwritten by an older checkpoint.

The existing filesystem checkpoint format may be read during migration, but new checkpoints are written to SQLite. A successful import marks the source checkpoint as migrated without deleting it automatically.

## Request lifecycle

For each logical model turn:

1. Persist any pending steering as chat turns and update the authoritative amendment projection.
2. Select the work class and its minimum/preferred output tier.
3. Select the smallest phase-appropriate tool bundle and include its schema cost in the estimate.
4. Build a candidate context using the output tier's input allowance.
5. If it does not fit, remove optional context in priority order.
6. If needed and allowed, perform one bounded compaction and rebuild.
7. Ask `BudgetPolicy` for a decision using the final input estimate.
8. Atomically reserve the physical request and tokens, then persist the request and context epoch.
9. Dispatch the provider request.
10. Settle actual usage or zero model usage for a pre-generation rejection.
11. Persist a validated assistant turn and settled tool results.
12. Emit durable budget, tool-bundle, and progress events for the TUI.

No assistant or tool turn is considered resumable until its durable record commits.

## Resume behavior

`/resume TASK_ID` performs these checks:

1. Load the task, final result if present, latest chat sequence, latest committed checkpoint, budget snapshot, and unresolved operations.
2. If operations are unresolved, show recovery requirements and deny write authority.
3. If the prior run completed, restore the transcript in inspection mode and offer `Continue` as a new linked task or `Fork` with edited instructions.
4. If the prior run was interrupted or paused with no unresolved operation, reconstruct the latest committed context epoch and offer `Continue` in the same task.
5. Revalidate repository identity, workspace fingerprint, model availability, context/output limits, and remaining budgets before dispatch.
6. Mark file evidence stale when hashes changed; require rereading before edits.

Resume never automatically sends a model request. The user explicitly chooses Continue or Fork from the restored chat.

## TUI behavior

The run-state panel replaces the single usage string with:

- requests used and remaining
- reported input/output tokens
- reserved but unsettled tokens
- remaining total tokens
- protected verification/finalization reserve
- current context estimate and output cap
- latest checkpoint sequence

Normal task activity stays concise. Selecting the context section or using `/budget` opens the detailed per-request ledger.

A budget/context stop presents:

- the typed reason in plain language
- whether a provider request was sent
- the attempted input estimate and output minimum/cap
- remaining request and token capacity
- what optional content was omitted or compacted
- available next actions: retry with a smaller scope, continue from checkpoint, choose a larger-context model, or inspect/export the current patch

The message `latest tool turn exceeds the configured prompt budget` is replaced by `The latest tool result is too large for this model's remaining context. No model request was sent.` when that is the actual condition.

## Configuration

Economy configuration gains explicit output tiers and reserves while preserving existing defaults when fields are absent:

- minimum and preferred output per work class
- finalization token reserve
- verification token reserve
- maximum tool preview characters
- compaction recovery attempts, fixed at one by default

Values are validated against the active model profile. Invalid configurations fail during `/doctor` or task admission, not midway through a run.

## Migration and compatibility

The run database gains a schema version and transactional migrations. Before applying a migration, Ion creates a recoverable sibling backup. A database with a newer unsupported version opens read-only and reports an actionable error.

Existing `runs`, `events`, `operations`, and `request_dedup` data remains readable. Old sessions without chat turns continue to support inspection and task re-submission, but are labeled `legacy context unavailable` and cannot claim exact context resume.

## Security and privacy

- All new database and backup files remain owner-only.
- Credentials and provider authorization headers are never stored in chat turns, requests, checkpoints, or diagnostics.
- Tool result previews pass through existing redaction before persistence.
- Artifact references are internal opaque IDs; model-provided paths do not become arbitrary host reads.
- Restored repository content remains labeled data and cannot override system or user policy.
- A resumed task must reacquire workspace ownership and reconcile unknown side effects before mutation.

## Observability

Diagnostics and durable events record:

- budget decision and work class
- estimate, reservation, settlement, and remaining capacity
- context omissions and compaction outcome
- request dispatch status and provider usage availability
- resume source checkpoint and stale evidence count

Metrics distinguish estimated from reported tokens. Unknown provider usage remains unknown; it is not converted to zero. Product telemetry is not introduced by this design.

## Acceptance criteria

1. A required-context overflow sends no provider request, consumes no model tokens, and reports `context_overflow` with an actionable message.
2. A short tool or final action can be admitted with a reduced output cap when reserving the profile maximum would exceed the total token budget.
3. Provider-reported usage replaces the reservation for the matching physical attempt.
4. A provider HTTP rejection records a request attempt and zero model-token usage.
5. Optional memory and duplicate tool payloads are removed before required task, steering, constraints, or latest-turn content.
6. One bounded compaction recovery is attempted at most once for a logical turn.
7. User, assistant, and settled tool turns survive process restart in their original order.
8. Resume reconstructs the latest committed context epoch without sending a model request or replaying a tool operation.
9. Steering added after a checkpoint survives restart and remains authoritative after resume.
10. Unresolved operations continue to block resume and write admission.
11. Legacy sessions remain inspectable and are clearly marked as non-resumable context.
12. The TUI displays requests, input/output usage, reservations, remaining tokens, protected reserve, current output cap, and a human-readable stop reason.
13. Navigation can use `file_outline` to locate a relevant range without reading the whole file.
14. Verification can use `artifact_search` and `artifact_read` without injecting a complete command artifact into model context.
15. Multi-file exact edits can use bounded `patch_apply` only with current hash-guarded evidence.
16. Tool schemas are selected by phase and evidence; adding tools does not cause every request to include the full registry.

## Delivery boundaries

Implementation is divided into four independently testable slices:

1. Budget policy, ledger snapshots, and typed failure classification.
2. Output-aware context selection, artifact-backed previews, and bounded overflow recovery.
3. Durable chat turns, request/context records, SQLite checkpoints, migrations, and reconstruction.
4. Resume controls, transcript restoration, detailed budget view, and actionable error presentation.

Each slice must preserve existing verification, workspace ownership, and provider error behavior. The first two slices can ship before exact context resume; old `/resume` behavior remains until the persistence slice is complete.
