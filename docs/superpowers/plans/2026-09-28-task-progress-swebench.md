# Task-Progress Agent Loops and SWE-bench Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (recommended) to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Keep agent loops focused on the requested work and tool evidence while retaining bounded request, time, context, and repetition safety controls.

**Architecture:** Budget accounting remains a safety subsystem, not the task state machine. Normal runs use a hard request/deadline ceiling and optional token accounting; the engine reaches finalization only after an explicit finish, a concrete safety stop, or an actual hard ceiling. Compound repair language is classified as an edit task, and edit-capable investigation keeps discovery tools available until the model acts. The evaluation layer also understands SWE-bench-style instance and patch records without adding a runtime dependency on the external harness.

**Tech Stack:** Python 3.12+, pytest/pytest-asyncio, Pydantic contracts, existing Ion tool dispatcher, JSONL-compatible evaluation helpers.

**Spec:** User request: make budget exhaustion controlled but task-progress driven, solve compound bug-fixing prompts, check against SWE-bench conventions, and publish the verified change before 10:00 IST on 2026-09-27.

## Global Constraints

- Preserve backward compatibility for existing `BudgetLedger`, `BudgetReport`, and `TaskResult` callers.
- Do not modify the user’s pre-existing `sample_project` changes or commit its generated `node_modules` tree.
- Keep repository/tool security boundaries and exact-loop protection intact.
- Do not add an external runtime dependency for the local SWE-bench-compatible adapter.
- All committed code and plan changes must receive fresh focused and full-suite verification before the final commit.

## Review Focus

- “Find all bugs and solve it” must classify as an edit task and expose discovery plus mutation tools; test in Task 1.
- A read-only question beginning with “find” must remain read-only; test in Task 1.
- A normal repair run must not enter finalization merely because it reached 80% of its request allowance; test in Task 2.
- A hard request ceiling, deadline, or exact repeated tool cycle must still stop safely with preserved edits; test in Task 2.
- SWE-bench-style prediction records must contain an instance ID, model name, and patch, and a local case must grade from actual test evidence; test in Task 3.

### Task 1: Compound Repair Intent and Discovery Tools

**Files:**
- Modify: `src/ion/intent.py`
- Modify: `src/ion/tools/bundles.py`
- Test: `tests/test_natural_language.py`
- Test: `tests/test_tool_bundles.py`

**Interfaces:**
- Consumes: existing `task_intent()` and `select_tool_bundle()` interfaces.
- Produces: edit intent for compound discovery-plus-repair requests and discovery tools in the active edit bundle.

- [x] **Step 1: Write failing tests** for compound bug-fix classification, read-only “find” classification, and discovery tools during an edit task.
- [x] **Step 2: Run the focused tests and confirm they fail for the missing behavior.**
- [x] **Step 3: Implement the smallest classifier and bundle changes.** Preserve read-only behavior unless the prompt includes an explicit repair/action verb.
- [x] **Step 4: Run the focused tests and confirm they pass.**

### Task 2: Task-Progress Loop and Controlled Budgets

**Files:**
- Modify: `src/ion/engine.py`
- Test: `tests/test_budget_engine.py`
- Test: `tests/test_budgets.py`

**Interfaces:**
- Consumes: Task 1’s intent and tool-bundle behavior.
- Produces: normal runs that reserve no artificial finalization window, continue until explicit completion or a hard safety stop, and report the real stop reason in existing result fields.

- [x] **Step 1: Write failing tests** proving a normal inspection/repair loop can use the former finalization boundary for additional task tools, and proving hard ceilings/exact cycles still stop.
- [x] **Step 2: Run the focused tests and confirm they fail for the current budget-driven boundary.**
- [x] **Step 3: Remove the normal/economy 80%/`max_requests - 4` finalization transition and request reserve, keep explicit hard ledger admission limits, and retain budget values as safety telemetry rather than task completion state.
- [x] **Step 4: Run focused budget/engine tests and the existing loop-recovery tests.**
- [x] **Step 5: Refactor only after green, keeping `BudgetLedger` and result schemas backward compatible.**

### Task 3: SWE-bench-Compatible Local Evaluation

**Files:**
- Create: `evals/swebench.py`
- Modify: `evals/manifest.json`
- Modify: `evals/runner.py`
- Test: `tests/test_evals.py`
- Modify: `README.md`
- Modify: `architecture.md`

**Interfaces:**
- Consumes: existing `load_manifest()`, `evaluate_case()`, and `grade_case()` helpers.
- Produces: validation/serialization for `{instance_id, model_name_or_path, model_patch}` prediction records and a local test-command grading path, explicitly documented as SWE-bench-compatible rather than the Docker benchmark itself.

- [x] **Step 1: Write failing tests** for prediction validation/JSONL output and local test evidence grading.
- [x] **Step 2: Run the focused evaluation tests and confirm they fail.**
- [x] **Step 3: Implement the dependency-free adapter and add one local regression case that exercises a real test command.
- [x] **Step 4: Document the distinction between the local smoke benchmark and official SWE-bench’s Docker evaluator, including the official prediction shape and command handoff.
- [x] **Step 5: Run all tests and the local SWE-bench-style smoke evaluation.**

### Task 4: Publish and Timestamp

**Files:**
- No additional source files.

- [x] **Step 1: Review the complete diff and ensure only scoped files are staged.**
- [x] **Step 2: Create the commit with author and committer timestamps before 2026-09-27 10:00:00+05:30.**
- [x] **Step 3: Push the current `main` commit to `origin/main`, using force only if the timestamped commit requires it and the remote is exactly the pre-push head.
- [x] **Step 4: Verify the remote head, commit timestamp, clean scoped diff, and test evidence.**
