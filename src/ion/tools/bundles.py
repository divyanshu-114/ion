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
) -> tuple[str, ...]:
    """Select a small tool set from the current phase and observed evidence."""
    if phase == Phase.verify:
        names = ["diff_summary", "diff_inspect"]
        if allow_commands:
            names.append("command_start")
        if edit_intent:
            names.extend(("file_read", "file_outline", "write_file"))
            if observed_page_count > 0:
                names.append("edit_file")
                if target_hashes_available:
                    names.append("patch_apply")
    elif phase == Phase.finalize:
        names = ["diff_summary"]
    elif phase == Phase.plan:
        names = ["file_read", "diff_summary"]
    elif phase == Phase.act and edit_intent:
        names = ["file_read", "file_outline", "write_file", "diff_summary"]
        if observed_page_count > 0:
            names.append("edit_file")
            if target_hashes_available:
                names.append("patch_apply")
    else:
        names = ["repo_list", "repo_search", "file_outline", "file_read"]
    if has_artifacts and phase in {Phase.plan, Phase.act, Phase.verify, Phase.finalize}:
        names.extend(("artifact_search", "artifact_read"))
    names.append("finish_request")
    return tuple(names)
