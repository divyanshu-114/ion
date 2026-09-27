from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from ion.budget import BudgetError, BudgetFailureCode, BudgetLedger
from ion.budget_policy import BudgetPolicy, WorkClass
from ion.config import AppConfig, resolve_profile
from ion.context import ContextManager, ContextOverflowError
from ion.contracts import BudgetReport, EngineEvent, ModelEvent, ModelRequest, OperationStatus, Outcome, Phase, TaskResult, TaskSpec, ToolCall, ToolResult, VerificationEvidence, VerificationRecord
from ion.diagnostics import DiagnosticLogger
from ion.gateway import ModelGateway, profile_digest
from ion.instructions import InstructionResolver
from ion.protocols import FinishAction, ToolAction, parse_action
from ion.tools.bundles import select_tool_bundle
from ion.tools.registry import ToolDispatcher, tool_schemas
from ion.verification import CompletionGate, observe_command
from ion.working_memory import LoopGuard, WorkingMemory
from ion.memory.retrieval import MemoryRetriever


class Engine:
    def __init__(self, config: AppConfig, gateway: ModelGateway, dispatcher: ToolDispatcher, profile_override=None, diagnostics: DiagnosticLogger | None = None, operation_store=None, memory_store=None) -> None:
        self.config = config
        self.gateway = gateway
        self.dispatcher = dispatcher
        self.profile_override = profile_override
        self.budget = BudgetLedger()
        self.context = ContextManager()
        self.queue: asyncio.Queue[EngineEvent] = asyncio.Queue()
        self.cancelled = False
        self.pending_steering: list[str] = []
        self.applied_steering: list[str] = []
        self.diagnostics = diagnostics
        self.operation_store = operation_store
        self.memory_store = memory_store

    def _diagnose(self, event: str, **fields) -> None:
        if self.diagnostics:
            self.diagnostics.emit(event, **fields)

    @staticmethod
    def _tool_response(result, economy: bool = False, max_chars: int = 4000) -> str:
        data = dict(result.data)
        if economy and "read_id" in data:
            data.pop("sha256", None)
        body = data.get("text")
        if isinstance(body, str) and len(body) > max_chars:
            data["text"] = body[:max_chars]
            data["truncated"] = True
            if "fully_read" in data:
                data["fully_read"] = False
            if isinstance(data.get("offset"), int):
                next_offset = data["offset"] + max_chars
                data["next_offset"] = next_offset
                tool = "file_read" if "read_id" in data else "artifact_read"
                data["read_more"] = f"Call {tool} with offset={next_offset} to read the next unseen text."
        for key in ("output", "patch"):
            value = data.get(key)
            if isinstance(value, str) and len(value) > max_chars:
                marker = "\n… output shortened …\n"
                body_chars = max_chars - len(marker)
                head = body_chars // 2
                data[key] = value[:head] + marker + value[-(body_chars - head):]
                data["truncated"] = True
        payload = {"status": result.status.value, "data": data}
        if result.error:
            payload["error"] = result.error[:500]
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    async def events(self):
        while True:
            event = await self.queue.get()
            yield event
            if event.phase == Phase.finalize:
                break

    async def cancel(self) -> None:
        self.cancelled = True
        await self.dispatcher.supervisor.close()

    async def steer(self, instruction: str) -> None:
        if not instruction.strip():
            raise ValueError("steering instruction cannot be blank")
        self.pending_steering.append(instruction.strip())
        await self._emit(Phase.intake, f"User steering queued: {instruction.strip()[:200]}")

    async def _emit(self, phase: Phase, message: str) -> None:
        await self.queue.put(EngineEvent(phase=phase, message=message))

    async def run(self, task: TaskSpec) -> TaskResult:
        economy = task.mode == "product" and self.config.economy.enabled
        edit_intent = bool(re.search(r"\b(?:add|change|create|edit|fix|implement|make|remove|rename|replace|rewrite|update)\w*\b", task.text, re.IGNORECASE))
        create_intent = bool(re.search(r"\b(?:add|create|generate|write)\b", task.text, re.IGNORECASE))
        if economy:
            self.budget = BudgetLedger(max_requests=self.config.economy.max_requests,
                                       max_total_tokens=self.config.economy.max_total_tokens,
                                       reserve_verification=True)
        profile = self.profile_override or resolve_profile(self.config, task.profile_name, task.mode)
        self._diagnose("run.start", provider=profile.provider, model=profile.model_id,
                       mode=task.mode, economy=economy)
        workspace = self.dispatcher.workspace
        if Path(task.repo_path).resolve() != workspace.root:
            raise ValueError("task repository does not match workspace")
        creation_navigation_ready = not workspace._paths()
        instructions = InstructionResolver().resolve(workspace.root, [])
        instruction_text = "\n".join(f"{item.path} ({item.sha256}):\n{item.text}" for item in instructions)
        if economy:
            instruction_text = "\n".join(f"{item.path}:\n{item.text}" for item in instructions)
        history: list[dict] = [{"role": "user", "content": task.text}]
        records: list[VerificationRecord] = []
        summary = "No final result supplied"
        outcome = Outcome.unverified
        rate_limit_retries = 0
        reported_dropped_turns = 0
        action_reminders = 0
        inspected = False
        prompt_tokens = 0
        completion_tokens = 0
        memory = WorkingMemory(workspace, self.dispatcher.artifacts)
        repository_memory = ""
        if self.memory_store:
            repository_memory = MemoryRetriever.render(self.memory_store.query(f"repo:{workspace.root}", task.text, budget=8))
        loop_guard = LoopGuard()
        read_counts: dict[tuple, int] = {}
        progress_hint = ""
        repair_attempts = 0
        edit_phase_started = False
        expanded_output = bool(economy and re.search(r"\b(?:rewrite|replace|regenerate)\b", task.text, re.IGNORECASE))
        truncation_retries = 0
        forced_write = False
        edit_complete = False
        request_dispatched = False
        error_category: str | None = None
        last_estimate: int | None = None
        last_cap: int | None = None
        last_protected = 0
        configured_tiers = {
            WorkClass.inspect: self.config.economy.inspect_output_tokens,
            WorkClass.edit: self.config.economy.edit_output_tokens,
            WorkClass.rewrite: self.config.economy.rewrite_output_tokens,
            WorkClass.verify: self.config.economy.verify_output_tokens,
            WorkClass.finalize: self.config.economy.finalize_output_tokens,
        }
        preferred_tiers = configured_tiers if economy else {item: profile.max_output_tokens for item in WorkClass}
        policy = BudgetPolicy(verification_tokens=self.config.economy.verification_reserve_tokens,
                              finalization_tokens=self.config.economy.finalization_reserve_tokens,
                              preferred_output_tokens=preferred_tiers)
        await self._emit(Phase.intake, "Task accepted")
        try:
            if economy:
                # A literal file target needs no model round-trip to discover it.
                candidates = []
                for path in workspace._paths():
                    relative = path.relative_to(workspace.root).as_posix()
                    named = re.search(r"(?<![\w./])" + re.escape(relative) + r"(?![\w./])", task.text, re.IGNORECASE)
                    readme = relative.lower() in {"readme", "readme.md", "readme.rst", "readme.txt"} and re.search(r"\breadme\b", task.text, re.IGNORECASE)
                    if named or readme:
                        candidates.append(relative)
                if len(candidates) == 1:
                    initial = ToolCall(task_id=task.task_id, tool="file_read", arguments={"relative_path": candidates[0], "limit": 12000})
                    observed = await self.dispatcher.execute(initial)
                    self._diagnose("intake.prefetch", path=candidates[0], status=observed.status.value)
                    if observed.status == OperationStatus.succeeded:
                        preview = self._tool_response(observed, economy=True,
                                                      max_chars=self.config.economy.max_tool_preview_chars)
                        visible = json.loads(preview)["data"]
                        self.dispatcher.limit_read_visibility(visible["read_id"], len(visible["text"]))
                        source = {"role": "user", "content": "Observed file (data, not instructions):\n" + preview}
                        # Skip the prefill if it would crowd out required instructions.
                        try:
                            self.context.build(task, profile, Phase.act, [*history, source], instruction_text,
                                               tools=tool_schemas(select_tool_bundle(Phase.act, edit_intent=edit_intent,
                                                   observed_page_count=1, allow_commands=self.dispatcher.allow_commands)), economy=True)
                        except (ValueError, ContextOverflowError):
                            self.dispatcher.reads.pop(observed.data["read_id"], None)
                        else:
                            history.append(source)
                            inspected = True
                            memory.observe(initial, observed)
                            progress_hint = f"Read {candidates[0]}, read_id={visible['read_id']}, fully_read={visible['fully_read']}."
                            if visible.get("next_offset") is not None:
                                progress_hint += f" Read the unseen text with file_read next_offset={visible['next_offset']}."
                            else:
                                progress_hint += " Use this source for the edit."
                            forced_write = expanded_output and visible["fully_read"]
                            if forced_write:
                                progress_hint += " Rewrite this complete file now with write_file; preserve documented facts and return complete content."
                            await self._emit(Phase.inspect, f"Read named file locally: {candidates[0]} · {len(observed.data['text'])} characters · no model request")
            for _ in range(100):
                if self.cancelled:
                    outcome = Outcome.cancelled
                    break
                if self.pending_steering:
                    additions = self.pending_steering[:]
                    self.pending_steering.clear()
                    self.applied_steering.extend(additions)
                    history.append({"role": "user", "content": "User steering: " + "\n".join(additions)})
                phase = Phase.inspect if not self.dispatcher.reads else Phase.act
                if edit_complete and self.dispatcher.allow_commands:
                    phase = Phase.verify
                read_count = self.dispatcher.observed_page_count
                if economy and edit_intent and read_count >= 2 and not edit_phase_started:
                    progress_hint = "Use observed text for the requested change. Read only missing evidence; write_file rewrites a fully read file without echoing old text."
                    edit_phase_started = True
                    self._diagnose("phase.transition", from_phase="inspect", to_phase="edit",
                                   observed_pages=read_count, history_reset=False)
                    await self._emit(Phase.plan, "Inspection complete; preparing a focused edit")
                remaining = self.budget.snapshot().remaining_tokens
                finalization_needed = (remaining is not None and last_estimate is not None and
                                       remaining < last_estimate + configured_tiers[WorkClass.inspect] +
                                       policy.verification_tokens + policy.finalization_tokens)
                if not edit_intent and inspected and (self.budget.used >= int(self.budget.max_requests * 0.8)
                                                       or finalization_needed):
                    phase = Phase.finalize
                names = select_tool_bundle(phase, edit_intent=edit_intent,
                                           observed_page_count=read_count,
                                           has_artifacts=self.dispatcher.artifacts.has_artifacts(),
                                           target_hashes_available=bool(self.dispatcher.reads),
                                           allow_commands=self.dispatcher.allow_commands)
                create_ready = create_intent and creation_navigation_ready
                if phase == Phase.inspect and create_ready:
                    names += ("write_file",)
                if phase == Phase.act and workspace.writes and self.dispatcher.allow_commands:
                    names += ("command_start",)
                schemas = tool_schemas(names)
                offered_names = [item["function"]["name"] for item in schemas]
                pointers = memory.render(economy=economy)
                combined_memory = "\n".join(item for item in (repository_memory, pointers) if item)
                if phase == Phase.verify:
                    work_class = WorkClass.verify
                elif phase == Phase.finalize:
                    work_class = WorkClass.finalize
                elif expanded_output and (read_count or create_ready):
                    work_class = WorkClass.rewrite
                elif edit_intent and (read_count or create_ready):
                    work_class = WorkClass.edit
                else:
                    work_class = WorkClass.inspect
                snapshot = self.budget.snapshot()
                initial_plan = policy.plan(work_class, 0, profile, snapshot)
                cap = initial_plan.output_cap
                input_allowance = (None if snapshot.remaining_tokens is None else
                                   snapshot.remaining_tokens - initial_plan.minimum_output_tokens - initial_plan.protected_tokens)
                compaction_recoveries = 0
                for _planning_pass in range(4):
                    try:
                        packet = self.context.build(
                            task, profile, phase, history, instruction_text, tuple(self.applied_steering),
                            memory=combined_memory, progress=progress_hint, tools=schemas, economy=economy,
                            output_cap=cap, input_budget_tokens=input_allowance,
                        )
                    except ContextOverflowError:
                        if cap == initial_plan.minimum_output_tokens or compaction_recoveries >= self.config.economy.compaction_recoveries:
                            raise
                        compaction_recoveries += 1
                        cap = initial_plan.minimum_output_tokens
                        await self._emit(phase, "Context overflow; retrying compaction once with the minimum useful output cap")
                        continue
                    last_estimate = packet.estimated_input_tokens
                    plan = policy.plan(work_class, packet.estimated_input_tokens, profile, self.budget.snapshot())
                    if plan.output_cap >= cap:
                        break
                    cap = plan.output_cap
                else:
                    raise BudgetError(BudgetFailureCode.token_limit, "output plan did not converge")
                last_cap = packet.max_output_tokens
                last_protected = plan.protected_tokens
                self._diagnose("request.prepare", phase=phase.value, request=self.budget.used + 1,
                               offered_tools=offered_names, reads=read_count,
                               estimated_input_tokens=packet.estimated_input_tokens,
                               output_cap=packet.max_output_tokens,
                               tool_choice="write_file" if forced_write else "auto",
                               accounted_tokens=self.budget.tokens_used,
                               token_limit=self.budget.max_total_tokens)
                try:
                    reservation = self.budget.admit(phase, packet.estimated_input_tokens, packet.max_output_tokens,
                                                    protected_tokens=plan.protected_tokens)
                except BudgetError:
                    # Admission may race another reservation; plan once from the current ledger.
                    snapshot = self.budget.snapshot()
                    plan = policy.plan(work_class, packet.estimated_input_tokens, profile, snapshot)
                    if plan.output_cap < packet.max_output_tokens:
                        packet = self.context.build(task, profile, phase, history, instruction_text,
                                                    tuple(self.applied_steering), memory=combined_memory,
                                                    progress=progress_hint, tools=schemas, economy=economy,
                                                    output_cap=plan.output_cap, input_budget_tokens=input_allowance)
                        plan = policy.plan(work_class, packet.estimated_input_tokens, profile, self.budget.snapshot())
                    reservation = self.budget.admit(phase, packet.estimated_input_tokens, packet.max_output_tokens,
                                                    protected_tokens=plan.protected_tokens)
                    last_estimate, last_cap, last_protected = packet.estimated_input_tokens, packet.max_output_tokens, plan.protected_tokens
                if packet.dropped_turns > reported_dropped_turns:
                    reported_dropped_turns = packet.dropped_turns
                    await self._emit(Phase.act, f"Context trimmed: {reported_dropped_turns} older tool turns omitted")
                request = ModelRequest(messages=packet.messages, tools=schemas if profile.tool_protocol == "native" else (), max_output_tokens=packet.max_output_tokens, profile_digest=profile_digest(profile), tool_choice="write_file" if forced_write and profile.tool_protocol == "native" else None)
                request_reserve = packet.estimated_input_tokens + packet.max_output_tokens
                remaining_after = None if snapshot.remaining_tokens is None else snapshot.remaining_tokens - request_reserve
                await self._emit(phase, f"Request {self.budget.used}/{self.budget.max_requests} · ~{packet.estimated_input_tokens} input · {packet.max_output_tokens} cap · {request_reserve} reserved · {snapshot.settled_tokens} settled · {remaining_after} remaining · {plan.protected_tokens} protected")
                text_parts: list[str] = []
                calls: list[ModelEvent] = []
                error = None
                retry_after_seconds: float | None = None
                request_usage = None
                finish_reason = None
                request_dispatched = True
                async for event in self.gateway.generate(request):
                    if event.finish_reason:
                        finish_reason = event.finish_reason
                    if event.usage:
                        request_usage = event.usage
                        prompt_tokens += event.usage.get("prompt_tokens", 0)
                        completion_tokens += event.usage.get("completion_tokens", 0)
                    if event.kind == "text_delta":
                        text_parts.append(event.text)
                    elif event.kind == "tool_call":
                        calls.append(event)
                    elif event.kind == "error":
                        error = event.error or "provider error"
                        retry_after_seconds = event.retry_after_seconds
                if request_usage and {"prompt_tokens", "completion_tokens"} <= request_usage.keys():
                    self.budget.settle(request_usage["prompt_tokens"], request_usage["completion_tokens"], reservation.attempt)
                    settled = self.budget.snapshot()
                    await self._emit(phase, f"Request {reservation.attempt} settled · {settled.settled_tokens} used · {settled.remaining_tokens} remaining")
                if (finish_reason in {"length", "max_tokens"} or
                        (request_usage or {}).get("completion_tokens", 0) >= request.max_output_tokens) and not error:
                    error = "provider output truncated"
                    calls.clear()
                self._diagnose("model.response", request=self.budget.used,
                               tools=[item.tool for item in calls if item.tool],
                               text_chars=sum(len(item) for item in text_parts), error=error,
                               prompt_tokens=(request_usage or {}).get("prompt_tokens"),
                               completion_tokens=(request_usage or {}).get("completion_tokens"),
                               reasoning_tokens=(request_usage or {}).get("reasoning_tokens"),
                               finish_reason=finish_reason,
                               accounted_tokens=self.budget.tokens_used)
                if error:
                    if forced_write and error == "provider rejected request (400)" and repair_attempts < 1:
                        forced_write = False
                        repair_attempts += 1
                        await self._emit(phase, "Provider rejected directed tool selection; retrying with automatic selection")
                        continue
                    if economy and error == "provider output truncated":
                        if truncation_retries == 0:
                            truncation_retries += 1
                            expanded_output = True
                            progress_hint = "Previous output was cut off; no actions were applied. Send a complete concise action. Use write_file for full rewrites, edit_file for small changes."
                            self._diagnose("request.retry", reason="output_truncated", next_output_cap=profile.max_output_tokens)
                            await self._emit(phase, "Output truncated; retrying once with room for a complete edit")
                            continue
                        summary = "Model output was truncated twice; no partial action was executed. Increase the profile output limit or request a smaller edit."
                        outcome = Outcome.blocked
                        error_category = "output_truncated"
                        break
                    if economy and error == "malformed provider response" and repair_attempts < 1:
                        repair_attempts += 1
                        progress_hint = "Previous response was malformed. Return one complete, valid tool action; keep the edit small."
                        await self._emit(phase, "Malformed response; allowing one repair attempt")
                        continue
                    if error == "provider rate limit" and rate_limit_retries < (1 if economy else 2):
                        retry_seconds = retry_after_seconds if retry_after_seconds is not None else float(rate_limit_retries + 1)
                        if retry_seconds > 30:
                            summary = f"Provider rate limit; retry after about {int(retry_seconds)} seconds"
                            outcome = Outcome.failed
                            error_category = "provider_rate_limit"
                            break
                        rate_limit_retries += 1
                        delay = max(1, int(retry_seconds + 0.999))
                        await self._emit(Phase.act, f"Provider rate limit; retrying in {delay}s")
                        await asyncio.sleep(delay)
                        continue
                    if error == "provider quota exhausted":
                        summary = "Provider quota exhausted. Choose another available model or wait for the provider quota to reset."
                        outcome = Outcome.failed
                        error_category = "provider_quota_exhausted"
                        break
                    summary = error
                    outcome = Outcome.failed
                    error_category = "provider_" + error.removeprefix("provider ").replace(" ", "_")
                    break
                rate_limit_retries = 0
                expanded_output = False
                forced_write = False
                content = "".join(text_parts)
                if profile.tool_protocol == "structured_json":
                    try:
                        action = parse_action(content)
                        if isinstance(action, ToolAction):
                            calls.append(ModelEvent(kind="tool_call", tool=action.tool, arguments=action.arguments, call_id="structured"))
                        else:
                            calls.append(ModelEvent(kind="tool_call", tool="finish_request", arguments={"summary": action.summary}, call_id="structured"))
                    except ValueError:
                        if economy:
                            repair_attempts += 1
                            if repair_attempts > 1:
                                summary = "Model returned invalid actions after one repair attempt"
                                outcome = Outcome.blocked
                                error_category = "malformed_action"
                                break
                        history.append({"role": "assistant", "content": content[:500] if economy else content})
                        history.append({"role": "user", "content": "Return exactly one valid JSON action object."})
                        continue
                if calls:
                    signature = json.dumps([(call.tool, call.arguments) for call in calls], sort_keys=True, separators=(",", ":"))
                    loop_state = loop_guard.observe(signature, workspace.fingerprint())
                    if loop_state == "stop":
                        summary = "Stopped repeated tool cycle without workspace progress"
                        outcome = Outcome.blocked
                        error_category = "repeated_tool_cycle"
                        break
                    if loop_state == "warn":
                        history.append({"role": "user", "content": "Repeated tool cycle detected. Choose a different query/file or act on existing evidence. Do not repeat the same actions; report a blocker with finish_request if stuck."})
                        await self._emit(Phase.act, "Repeated tool cycle detected; asking model to change approach")
                        continue
                    if len(calls) > 1 and any(event.tool == "finish_request" for event in calls):
                        summary = "finish_request must be the only tool call in its response"
                        outcome = Outcome.blocked
                        error_category = "invalid_tool_batch"
                        break
                    finish_requested = False
                    assistant: dict = {"role": "assistant", "content": content or None}
                    if profile.tool_protocol == "native":
                        assistant["tool_calls"] = [{"id": call.call_id, "type": "function", "function": {"name": call.tool, "arguments": json.dumps(call.arguments)}} for call in calls]
                    history.append(assistant)
                    for event in calls:
                        call = ToolCall(task_id=task.task_id, tool=event.tool or "", arguments=event.arguments or {})
                        safe_detail = {}
                        if call.tool == "file_read":
                            safe_detail = {"path": str(call.arguments.get("relative_path", ""))[:300],
                                           "offset": call.arguments.get("offset", 0)}
                        elif call.tool == "repo_list":
                            safe_detail = {"path": str(call.arguments.get("relative_path", ""))[:300]}
                        elif call.tool == "repo_search":
                            safe_detail = {"path": str(call.arguments.get("relative_path", ""))[:300],
                                           "query_chars": len(str(call.arguments.get("query", "")))}
                        elif call.tool in {"edit_file", "write_file"}:
                            safe_detail = {"read_id": str(call.arguments.get("read_id", ""))[:40],
                                           "old_chars": len(str(call.arguments.get("old_text", ""))),
                                           "new_chars": len(str(call.arguments.get("new_text", call.arguments.get("content", "")))),
                                           "plan_chars": len(str(call.arguments.get("plan", ""))),
                                           "done": bool(call.arguments.get("done"))}
                        self._diagnose("tool.request", tool=call.tool, **safe_detail)
                        detail = ""
                        if call.tool == "file_read":
                            detail = f" · {call.arguments.get('relative_path', '')} · offset {call.arguments.get('offset', 0)}"
                        await self._emit(phase, f"{call.tool} requested{detail}")
                        if call.tool not in names:
                            unavailable_error = "Use the offered tools; this command is outside the current execution policy."
                            result = ToolResult(operation_id=call.operation_id, status=OperationStatus.failed,
                                                summary="tool unavailable", error=unavailable_error)
                            self._diagnose("tool.result", tool=call.tool, status=result.status.value,
                                           error=result.error)
                            response = self._tool_response(result, economy=economy,
                                                           max_chars=self.config.economy.max_tool_preview_chars)
                            if profile.tool_protocol == "native":
                                history.append({"role": "tool", "tool_call_id": event.call_id, "content": response})
                            else:
                                history.append({"role": "user", "content": "Tool result: " + response})
                            repair_attempts += 1
                            if repair_attempts > 1:
                                summary = "Model repeatedly requested unavailable tools"
                                outcome = Outcome.blocked
                                error_category = "unavailable_tool"
                                finish_requested = True
                                break
                            continue
                        if call.tool == "finish_request":
                            if not inspected:
                                if action_reminders >= 2:
                                    summary = "Model repeatedly finished without inspecting the repository"
                                    outcome = Outcome.blocked
                                    error_category = "missing_inspection"
                                    finish_requested = True
                                    break
                                action_reminders += 1
                                response = '{"error":"Inspect the relevant file with file_read or repo_search before finishing. Perform the requested task using tools."}'
                                if profile.tool_protocol == "native":
                                    history.append({"role": "tool", "tool_call_id": event.call_id, "content": response})
                                else:
                                    history.append({"role": "user", "content": response})
                                await self._emit(Phase.act, "Asked model to inspect the repository before finishing")
                                continue
                            summary = str(call.arguments.get("summary", content))
                            if economy and edit_intent and not workspace.writes:
                                summary = "No edits were applied. " + summary
                                outcome = Outcome.blocked
                                error_category = "no_edit_applied"
                            finish_requested = True
                            if self.pending_steering:
                                result = await self.dispatcher.execute(call)
                                response = self._tool_response(result, economy=economy,
                                                               max_chars=self.config.economy.max_tool_preview_chars)
                                if profile.tool_protocol == "native":
                                    history.append({"role": "tool", "tool_call_id": event.call_id, "content": response})
                                else:
                                    history.append({"role": "user", "content": f"Tool result: {response}"})
                            break
                        if economy and call.tool in {"edit_file", "write_file"}:
                            await self._emit(Phase.plan, str(call.arguments.get("plan", ""))[:600])
                        if self.operation_store:
                            self.operation_store.prepare_operation(task.task_id, call.operation_id, call.tool, call.arguments)
                            self.operation_store.start_operation(task.task_id, call.operation_id)
                        try:
                            result = await self.dispatcher.execute(call)
                        except asyncio.CancelledError:
                            # Leave the durable operation in running state. A
                            # later reconciler must inspect the workspace before
                            # granting another writer.
                            raise
                        except Exception as exc:
                            if self.operation_store:
                                self.operation_store.settle_operation(task.task_id, call.operation_id, {"error": str(exc)}, "unknown")
                            raise
                        if self.operation_store:
                            self.operation_store.settle_operation(
                                task.task_id,
                                call.operation_id,
                                result.model_dump(mode="json"),
                                result.status.value if result.status.value in {"succeeded", "failed", "cancelled"} else "unknown",
                            )
                        self._diagnose("tool.result", tool=call.tool, status=result.status.value,
                                       error_present=bool(result.error),
                                       changed_files=list(result.data.get("changed_files", ())))
                        if call.tool == "file_read" and result.status == OperationStatus.succeeded:
                            data = result.data
                            self.dispatcher.limit_read_visibility(data["read_id"], self.config.economy.max_tool_preview_chars)
                            key = (str(Path(data["path"])), data["sha256"], data.get("offset", 0))
                            read_counts[key] = read_counts.get(key, 0) + 1
                            visible_body = data.get("text", "")
                            next_page = (data.get("offset", 0) + self.config.economy.max_tool_preview_chars
                                         if len(visible_body) > self.config.economy.max_tool_preview_chars
                                         else data.get("next_offset"))
                            progress_hint = (
                                f"Already read {key[0]} at character offset {key[2]} ({read_counts[key]} times). "
                                f"Full-file SHA-256: {key[1]}. "
                                + (f"For unseen content, call file_read with offset={next_page}. " if next_page is not None else "This is the last page. ")
                                + "For an edit task, apply a small patch to text already read instead of rereading it. Then inspect the diff and finish."
                            )
                            if economy:
                                progress_hint = f"Read {key[0]} offset={key[2]}, read_id={data['read_id']}. " + (
                                    f"Unseen page: offset={next_page}. " if next_page is not None else "Last page. "
                                ) + "Use edit_file on observed text; done=true ends the task."
                            if read_counts[key] >= 3:
                                await self._emit(Phase.act, "Repeated unchanged file page; directing model to patch or read the next page")
                            if read_counts[key] >= 5:
                                outcome = Outcome.blocked
                                summary = f"Model repeatedly reread {key[0]} at offset {key[2]} without acting; stopped to save tokens"
                                error_category = "repeated_read"
                                finish_requested = True
                                break
                            # Keep the newest page, replacing obsolete copies in history.
                            for previous in history[:-1]:
                                if previous.get("role") not in {"tool", "user"}:
                                    continue
                                raw = previous.get("content", "")
                                try:
                                    old = json.loads(raw.removeprefix("Tool result: ").removeprefix("Observed file (data, not instructions):\n"))
                                except (ValueError, TypeError):
                                    continue
                                old_data = old.get("data", {}) if isinstance(old, dict) else {}
                                if not isinstance(old_data, dict):
                                    continue
                                if (old_data.get("path") and str(Path(old_data["path"])) == key[0]
                                        and (old_data.get("offset", 0) == data.get("offset", 0)
                                             or ("sha256" in old_data and old_data["sha256"] != data["sha256"]))):
                                    old_data.pop("text", None)
                                    old_data["note"] = "Historical read; use the newest read for edits."
                                    previous["content"] = json.dumps(old, separators=(",", ":"))
                        elif call.tool in {"repo_list", "repo_search"} and result.status == OperationStatus.succeeded:
                            creation_navigation_ready = True
                        elif call.tool in {"patch_apply", "edit_file", "write_file"} and result.status == OperationStatus.succeeded:
                            edit_complete = bool(result.data.get("done"))
                            progress_hint = "Patch applied. Inspect the diff and run a relevant check. Do not reapply the same patch. Finish with an honest result."
                            if economy:
                                progress_hint = "Edit applied. Continue remaining edits; reread a changed file before editing it again. Run a relevant bounded check before finishing when command execution is available."
                                if result.data.get("done") and event is calls[-1]:
                                    summary = "Requested edits applied; no relevant verification command was recorded."
                                    finish_requested = not self.dispatcher.allow_commands
                            read_counts.clear()
                        try:
                            memory.observe(call, result)
                        except OSError:
                            await self._emit(Phase.act, "Memory file unavailable; continuing with in-process pointers")
                        if self.memory_store and result.status == OperationStatus.succeeded:
                            if call.tool == "file_read" and result.data.get("path") and result.data.get("sha256"):
                                self.memory_store.observe({
                                    "scope": f"repo:{workspace.root}",
                                    "fact_key": f"file:{result.data['path']}",
                                    "text": f"Observed {result.data['path']} at hash {result.data['sha256']}",
                                    "evidence_kind": "observed",
                                    "source_refs": [f"{result.data['path']}:{result.data['sha256']}"],
                                    "supporting_hashes": [result.data["sha256"]],
                                })
                            elif call.tool == "command_start" and result.data.get("exit_code") == 0:
                                self.memory_store.observe({
                                    "scope": f"repo:{workspace.root}",
                                    "fact_key": "command.last_success",
                                    "text": str(call.arguments.get("command", ""))[:500],
                                    "evidence_kind": "observed",
                                    "source_refs": [str(result.data.get("artifact_id", call.operation_id))],
                                })
                        if result.status == OperationStatus.succeeded and call.tool in {"file_read", "repo_search", "diff_inspect", "command_start"}:
                            inspected = True
                        if result.status == OperationStatus.failed:
                            await self._emit(Phase.act, f"{call.tool} failed: {(result.error or '')[:200]}")
                            if economy:
                                repair_attempts += 1
                                if repair_attempts > 1:
                                    summary = "Tool failed after one repair attempt: " + (result.error or "unknown error")[:200]
                                    outcome = Outcome.blocked
                                    error_category = "tool_failure"
                                    finish_requested = True
                        if call.tool == "command_start" and result.status == OperationStatus.succeeded:
                            data = result.data
                            if int(data.get("exit_code") or 0) != 0:
                                edit_complete = False
                            record = observe_command(str(call.arguments.get("command", "")), str(data.get("output", "")), int(data.get("exit_code") or 0), call.operation_id, workspace.fingerprint(), workspace.changes().changed_files, task.criteria or ("task",))
                            if record:
                                records.append(record)
                        response = self._tool_response(result, economy=economy,
                                                       max_chars=self.config.economy.max_tool_preview_chars)
                        if profile.tool_protocol == "native":
                            history.append({"role": "tool", "tool_call_id": event.call_id, "content": response})
                        else:
                            history.append({"role": "user", "content": f"Tool result: {response}"})
                        if finish_requested:
                            break
                    if finish_requested:
                        if self.pending_steering:
                            continue
                        break
                else:
                    if action_reminders < 2:
                        action_reminders += 1
                        history.append({"role": "assistant", "content": content or "(empty response)"})
                        history.append({"role": "user", "content": "Use an offered tool for the next step; finish_request reports an honest result or blocker."})
                        await self._emit(Phase.act, "Asked model to continue with tools")
                        continue
                    summary = "Model stopped using tools before completing the task"
                    outcome = Outcome.blocked
                    error_category = "no_tool_action"
                    break
            else:
                outcome = Outcome.budget_exhausted
                error_category = "request_limit"
        except ContextOverflowError as exc:
            outcome = Outcome.blocked
            error_category = "context_overflow"
            last_estimate = exc.manifest.estimated_input_tokens
            last_cap = exc.manifest.output_cap
            if exc.code == "latest_turn_overflow":
                summary = "The latest tool result is too large for this prompt budget. Rerun with a smaller read or narrower task, or choose a larger-context profile; "
            else:
                summary = "Required task context exceeds this prompt budget. Narrow the task or choose a larger-context profile; "
            summary += "no model request was sent." if not request_dispatched else "no model request was sent for this turn."
            self._diagnose("run.stop", reason=exc.code, outcome=outcome.value,
                           requests=self.budget.used, accounted_tokens=self.budget.tokens_used)
        except BudgetError as exc:
            context_failure = exc.code in {BudgetFailureCode.context_overflow, BudgetFailureCode.latest_turn_overflow}
            outcome = Outcome.blocked if context_failure or exc.code == BudgetFailureCode.verification_reserve else Outcome.budget_exhausted
            error_category = "context_overflow" if context_failure else exc.code.value
            summary = str(exc) + (f". {exc.suggested_action}" if exc.suggested_action else "")
            summary += "; no model request was sent." if not request_dispatched else "; no model request was sent for this turn."
            self._diagnose("run.stop", reason=exc.code.value, outcome=outcome.value,
                           requests=self.budget.used, accounted_tokens=self.budget.tokens_used)
        except (RuntimeError, ValueError) as exc:
            summary = str(exc)
            outcome = Outcome.blocked
            error_category = "engine_error"
            self._diagnose("run.stop", reason=summary, outcome=outcome.value,
                           requests=self.budget.used, accounted_tokens=self.budget.tokens_used)
        finally:
            await self.dispatcher.supervisor.close()
        checks_disabled = economy and not self.dispatcher.allow_commands
        await self._emit(Phase.verify, "Capturing final diff; repository checks disabled" if checks_disabled else "Checking final workspace")
        await self._emit(Phase.verify, f"Usage: {self.budget.used} requests · {prompt_tokens} reported input tokens · {completion_tokens} reported output tokens")
        if economy:
            await self._emit(Phase.verify, f"Usage: {self.budget.used}/{self.budget.max_requests} requests · {self.budget.tokens_used}/{self.budget.max_total_tokens} accounted tokens (reported usage or estimates)")
        fingerprint = workspace.fingerprint()
        changes = workspace.changes()
        if not edit_intent and not changes.changed_files and inspected and not records and self.dispatcher.reads:
            path, sha256, offset, _ = next(iter(self.dispatcher.reads.values()))
            records.append(VerificationRecord(
                criterion_ids=task.criteria or ("task",),
                evidence=VerificationEvidence(kind="static", source_refs=(f"{path}:{sha256}:{offset}",)),
                workspace_fingerprint=fingerprint,
                status="passed",
            ))
        patch = workspace.patch_text()
        patch_artifact = self.dispatcher.artifacts.put(patch.encode(), "ion_patch") if patch else None
        if outcome == Outcome.unverified:
            outcome = CompletionGate().decide(changes, records, fingerprint)
        limitations = (("Repository checks disabled; edits have not been tested.",) if checks_disabled else
                       (("No relevant executable verification was recorded.",) if outcome != Outcome.verified else ()))
        if economy:
            if changes.ambiguous_files:
                summary += " Concurrent changes detected; review the affected files."
        budget_snapshot = self.budget.snapshot()
        budget_report = BudgetReport(requests_used=budget_snapshot.requests_used,
                                     requests_remaining=budget_snapshot.requests_remaining,
                                     settled_tokens=budget_snapshot.settled_tokens,
                                     reserved_tokens=budget_snapshot.reserved_tokens,
                                     token_limit=budget_snapshot.token_limit,
                                     deadline_reached=budget_snapshot.deadline_reached,
                                     protected_tokens=last_protected,
                                     estimated_input_tokens=last_estimate,
                                     output_cap=last_cap)
        result = TaskResult(task_id=task.task_id, outcome=outcome, summary=summary, changed_files=changes.changed_files, patch_artifact_id=patch_artifact.artifact_id if patch_artifact else None, verification_ids=tuple(record.verification_id for record in records), limitations=limitations, final_workspace_fingerprint=fingerprint,
                            requests_used=self.budget.used, reported_input_tokens=prompt_tokens,
                            reported_output_tokens=completion_tokens, accounted_tokens=self.budget.tokens_used,
                            error_category=error_category, request_dispatched=request_dispatched,
                            budget=budget_report)
        self._diagnose("run.finish", outcome=outcome.value, changed_files=list(changes.changed_files),
                       attributable_files=list(changes.attributable_files), requests=self.budget.used,
                       reported_input_tokens=prompt_tokens, reported_output_tokens=completion_tokens,
                       accounted_tokens=self.budget.tokens_used)
        await self._emit(Phase.finalize, f"{outcome.value}: {summary}")
        return result
