from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from evals.grader import grade_case
from evals.swebench import run_local_check


def load_manifest(path: Path) -> list[dict]:
    data = json.loads(Path(path).read_text())
    if data.get("version") != 1 or not isinstance(data.get("cases"), list):
        raise ValueError("manifest must use version 1 and contain cases")
    cases = data["cases"]
    ids = [case.get("id") for case in cases]
    if any(not item for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("manifest contains duplicate or blank case id")
    if any(not isinstance(case.get("task"), str) or not case["task"].strip() for case in cases):
        raise ValueError("manifest case task cannot be blank")
    return cases


def evaluate_case(manifest_entry: dict, profile, execute: Callable | None = None) -> dict:
    """Run an injected case executor and return a serializable independent record."""
    if not manifest_entry.get("id"):
        raise ValueError("case id is required")
    if execute is None:
        return {"case_id": manifest_entry["id"], "status": "not_run", "reason": "no executor supplied"}
    result = execute(manifest_entry, profile)
    return {"case_id": manifest_entry["id"], "status": "completed", "result": result}


def evaluate_local_case(manifest_entry: dict, *, cwd: Path,
                        changed_files: tuple[str, ...], outcome: str,
                        test_command=None) -> dict:
    """Grade one case with a real local test command.

    This mirrors the evidence shape used by the product without claiming to
    replace SWE-bench's containerized evaluator.
    """
    command = test_command if test_command is not None else manifest_entry.get("test_command")
    if command is None:
        raise ValueError("local case requires test_command")
    check = run_local_check(command, cwd=cwd)
    grade = grade_case(
        manifest_entry,
        changed_files=changed_files,
        outcome=outcome,
        verification_ids=("local-test",) if check.passed else (),
    )
    return {
        "case_id": manifest_entry["id"],
        "status": "passed" if grade.passed else "failed",
        "check": {
            "passed": check.passed,
            "returncode": check.returncode,
            "output": check.output,
            "timed_out": check.timed_out,
        },
        "reasons": grade.reasons,
    }
