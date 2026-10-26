from __future__ import annotations

from ion.contracts import Phase


def select_tool_bundle(
    phase: Phase,
    *,
    edit_intent: bool = False,
    observed_page_count: int = 0,
    has_artifacts: bool = False,
    target_hashes_available: bool = False,
    allow_commands: bool = False,
    delete_intent: bool = False,
) -> tuple[str, ...]:
    """Select a small tool set from the current phase and observed evidence."""
    if phase == Phase.verify:
        names = ["diff_summary", "diff_inspect", "web_search"]
        if allow_commands:
            names.extend(("command_start", "run_linter"))
        if edit_intent:
            names.extend(("file_read", "file_outline", "write_file"))
            if observed_page_count > 0:
                names.append("edit_file")
                if delete_intent:
                    names.append("delete_file")
                if target_hashes_available:
                    names.append("patch_apply")
    elif phase == Phase.finalize:
        names = ["diff_summary"]
    elif phase == Phase.plan:
        names = ["file_read", "diff_summary"]
    elif phase == Phase.act and edit_intent:
        # Repair tasks need to keep discovering repository-wide evidence after
        # the first read. Tool availability follows the task, not a premature
        # phase transition caused by one observed page.
        names = ["repo_list", "repo_search", "infra_scan", "file_read", "file_outline",
                 "write_file", "diff_summary", "trace_symbol"]
        if allow_commands:
            names.append("run_linter")
        if observed_page_count > 0:
            names.append("edit_file")
            if delete_intent:
                names.append("delete_file")
            if target_hashes_available:
                names.append("patch_apply")
    else:
        names = ["repo_list", "repo_search", "file_outline", "file_read", "trace_symbol", "web_search", "infra_scan"]
    if has_artifacts:
        names.extend(("artifact_search", "artifact_read"))
    names.append("finish_request")
    return tuple(names)


def eligible_tools(*, edit_intent: bool, observed_page_count: int,
                   has_artifacts: bool, target_hashes_available: bool,
                   allow_commands: bool, delete_intent: bool = False) -> frozenset[str]:
    """Separate phase preferences from permission and evidence requirements."""
    return frozenset(name for phase in (Phase.inspect, Phase.act, Phase.verify, Phase.plan)
                     for name in select_tool_bundle(
                         phase, edit_intent=edit_intent, observed_page_count=observed_page_count,
                         has_artifacts=has_artifacts, target_hashes_available=target_hashes_available,
                         allow_commands=allow_commands, delete_intent=delete_intent))
