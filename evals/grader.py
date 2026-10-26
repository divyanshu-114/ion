from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Grade:
    passed: bool
    reasons: tuple[str, ...] = ()


def grade_case(case: dict, *, changed_files: tuple[str, ...], outcome: str, verification_ids: tuple[str, ...]) -> Grade:
    reasons: list[str] = []
    expected = set(case.get("expected_files", ()))
    if expected and not expected.issubset(set(changed_files)):
        reasons.append("expected files were not changed")
    if outcome not in {"verified", "passed"}:
        reasons.append("task did not finish verified")
    if case.get("required_check", True) and not verification_ids:
        reasons.append("independent verification evidence is missing")
    return Grade(not reasons, tuple(reasons))
