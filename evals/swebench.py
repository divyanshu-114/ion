"""Small, dependency-free helpers for SWE-bench-compatible local checks.

The official SWE-bench evaluator applies the patch in an isolated container
and runs the instance's tests. Ion cannot reproduce that environment here, so
this module validates the official prediction record and provides a safe
argv-based local test runner for smoke cases.
"""
from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


_PREDICTION_FIELDS = frozenset({"instance_id", "model_name_or_path", "model_patch"})


@dataclass(frozen=True)
class Prediction:
    instance_id: str
    model_name_or_path: str
    model_patch: str

    def __post_init__(self) -> None:
        if not isinstance(self.instance_id, str) or not self.instance_id.strip():
            raise ValueError("instance_id cannot be blank")
        if not isinstance(self.model_name_or_path, str) or not self.model_name_or_path.strip():
            raise ValueError("model_name_or_path cannot be blank")
        if not isinstance(self.model_patch, str):
            raise ValueError("model_patch must be a string")

    @classmethod
    def from_mapping(cls, value: object) -> Prediction:
        if not isinstance(value, dict):
            raise ValueError("prediction must be a JSON object")
        fields = set(value)
        missing = _PREDICTION_FIELDS - fields
        if missing:
            raise ValueError(f"prediction missing fields: {', '.join(sorted(missing))}")
        extra = fields - _PREDICTION_FIELDS
        if extra:
            raise ValueError(f"prediction has unexpected fields: {', '.join(sorted(extra))}")
        return cls(
            instance_id=value["instance_id"],
            model_name_or_path=value["model_name_or_path"],
            model_patch=value["model_patch"],
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "instance_id": self.instance_id,
            "model_name_or_path": self.model_name_or_path,
            "model_patch": self.model_patch,
        }


def load_predictions(path: Path) -> tuple[Prediction, ...]:
    predictions: list[Prediction] = []
    seen: set[str] = set()
    for line_number, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            prediction = Prediction.from_mapping(json.loads(line))
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            raise ValueError(f"invalid prediction on line {line_number}: {exc}") from exc
        if prediction.instance_id in seen:
            raise ValueError(f"duplicate prediction instance_id: {prediction.instance_id}")
        seen.add(prediction.instance_id)
        predictions.append(prediction)
    return tuple(predictions)


def serialize_predictions(path: Path, predictions: Sequence[Prediction]) -> None:
    records: list[str] = []
    seen: set[str] = set()
    for prediction in predictions:
        if prediction.instance_id in seen:
            raise ValueError(f"duplicate prediction instance_id: {prediction.instance_id}")
        seen.add(prediction.instance_id)
        records.append(json.dumps(prediction.as_dict(), ensure_ascii=False, separators=(",", ":")))
    Path(path).write_text("\n".join(records) + ("\n" if records else ""))


@dataclass(frozen=True)
class LocalCheck:
    passed: bool
    returncode: int | None
    output: str
    timed_out: bool = False


def run_local_check(command: Sequence[str] | str, *, cwd: Path, timeout: float = 300) -> LocalCheck:
    """Run a trusted local benchmark command without invoking a shell."""
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    argv = shlex.split(command) if isinstance(command, str) else list(command)
    if not argv or any(not isinstance(item, str) or not item for item in argv):
        raise ValueError("command must contain a non-empty argv")
    try:
        completed = subprocess.run(
            [str(item) for item in argv], cwd=Path(cwd), capture_output=True,
            text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = "".join(part if isinstance(part, str) else part.decode(errors="replace")
                          for part in (exc.stdout, exc.stderr) if part)
        return LocalCheck(False, None, output, timed_out=True)
    output = completed.stdout + completed.stderr
    return LocalCheck(completed.returncode == 0, completed.returncode, output)
