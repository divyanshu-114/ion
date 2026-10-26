from __future__ import annotations

import re
import shlex
from pathlib import Path

from ion.contracts import Outcome, VerificationEvidence, VerificationRecord
from ion.workspace import ChangeSet


class CompletionGate:
    def decide(self, changed: ChangeSet, records: list[VerificationRecord], final_fingerprint: str) -> Outcome:
        if changed.ambiguous_files:
            return Outcome.unverified
        if not changed.attributable_files:
            if not changed.changed_files and any(
                record.status == "passed"
                and record.evidence.kind == "static"
                and record.workspace_fingerprint == final_fingerprint
                for record in records
            ):
                return Outcome.verified
            return Outcome.unverified
        if not any(record.status == "passed" and record.workspace_fingerprint == final_fingerprint for record in records):
            return Outcome.unverified
        return Outcome.verified


def observe_pytest(command: str, output: str, exit_code: int, operation_id: str, fingerprint: str, changed_files: tuple[str, ...], criterion_ids: tuple[str, ...] = ("task",)) -> VerificationRecord | None:
    if "pytest" not in command or exit_code != 0:
        return None
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if any(part in {"&&", "||", ";", "|", ">", "<"} for part in parts):
        return None
    pytest_entry = parts[0] == "pytest" or (len(parts) >= 3 and Path(parts[0]).name.startswith("python") and parts[1:3] == ["-m", "pytest"])
    if not pytest_entry:
        return None
    targets = [part for part in parts if part.endswith(".py") or part.startswith("tests/")]
    if not targets:
        return None
    relevant = False
    for changed in changed_files:
        stem = Path(changed).stem.removeprefix("test_")
        relevant |= any(stem in Path(target).stem or changed == target for target in targets)
    if not relevant:
        return None
    match = re.search(r"(?m)^\s*(?:=+\s*)?(\d+) passed(?:\s|$)", output)
    if not match or int(match.group(1)) <= 0:
        return None
    return VerificationRecord(
        criterion_ids=criterion_ids,
        evidence=VerificationEvidence(kind="executable", command_operation_id=operation_id),
        workspace_fingerprint=fingerprint,
        status="passed",
    )


def _relevant_target(changed_files: tuple[str, ...], targets: list[str]) -> bool:
    """Require the successful command to cover at least one changed source area."""
    if not changed_files:
        return False
    if not targets:
        return True
    for changed in changed_files:
        changed_path = Path(changed)
        changed_stem = changed_path.stem.removeprefix("test_")
        if any(
            target == changed
            or target.startswith(changed.rstrip("/") + "/")
            or changed.startswith(target.rstrip("/") + "/")
            or changed_stem in Path(target).stem
            or Path(target).stem.removeprefix("test_") in changed_stem
            for target in targets
        ):
            return True
    return False


def _record(operation_id: str, fingerprint: str, criterion_ids: tuple[str, ...]) -> VerificationRecord:
    return VerificationRecord(
        criterion_ids=criterion_ids,
        evidence=VerificationEvidence(kind="executable", command_operation_id=operation_id),
        workspace_fingerprint=fingerprint,
        status="passed",
    )


def observe_command(command: str, output: str, exit_code: int, operation_id: str, fingerprint: str, changed_files: tuple[str, ...], criterion_ids: tuple[str, ...] = ("task",)) -> VerificationRecord | None:
    """Observe a successful, relevant repository check without trusting its prose.

    The observer deliberately accepts only common test runners and requires a
    nonzero test count or an unambiguous package success marker. Shell chains
    are rejected so an unrelated command cannot manufacture evidence.
    """
    if exit_code != 0 or not command.strip():
        return None
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if any(part in {"&&", "||", ";", "|", ">", "<"} for part in parts):
        return None
    if not parts:
        return None
    lower = command.lower()
    if "pytest" in lower:
        return observe_pytest(command, output, exit_code, operation_id, fingerprint, changed_files, criterion_ids)

    targets = [part for part in parts[1:] if not part.startswith("-") and ("/" in part or Path(part).suffix)]
    if any(part in {"./...", "...", "--all", "--workspace"} for part in targets):
        targets = []
    if not _relevant_target(changed_files, targets):
        return None
    normalized = output.lower()
    if re.search(r"\b0\s+(?:passed|tests?|specs?)\b", normalized) or "no tests found" in normalized:
        return None

    passed = False
    if parts[0] in {"cargo", "cargo.exe"} and len(parts) > 1 and parts[1] == "test":
        passed = bool(re.search(r"test result:\s*ok\.", normalized)) and not bool(re.search(r"\b0 passed\b", normalized))
    elif parts[0] in {"go", "go.exe"} and len(parts) > 1 and parts[1] == "test":
        passed = bool(re.search(r"(?m)^ok\s+\S+", output))
    elif parts[0] in {"npm", "pnpm", "yarn", "bun"} and "test" in parts:
        passed = bool(
            re.search(r"\b(?:tests?|specs?)?\s*(?:passed|passing)\b", normalized)
            or re.search(r"\btest suites?\s*:?\s*\d+\s+passed\b", normalized)
        ) and not any(marker in normalized for marker in ("failed", "failing", "error"))
    elif "unittest" in lower:
        passed = bool(re.search(r"(?m)^OK(?:\s|$)", output)) and not bool(re.search(r"\b0 tests?\b", normalized))
    if not passed:
        return None
    return _record(operation_id, fingerprint, criterion_ids)
