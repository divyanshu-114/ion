import json
import sys

import pytest

from evals.grader import grade_case
from evals.runner import evaluate_local_case, load_manifest
from evals.swebench import Prediction, load_predictions, run_local_check, serialize_predictions


def test_manifest_is_pinned_and_duplicate_ids_are_rejected(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"version": 1, "cases": [{"id": "one", "task": "inspect"}]}))
    assert load_manifest(manifest)[0]["id"] == "one"
    manifest.write_text(json.dumps({"version": 1, "cases": [{"id": "one", "task": "a"}, {"id": "one", "task": "b"}]}))
    with pytest.raises(ValueError, match="duplicate"):
        load_manifest(manifest)


def test_grader_requires_expected_change_and_passing_check():
    assert grade_case({"expected_files": ["bug.py"], "required_check": True}, changed_files=("bug.py",), outcome="verified", verification_ids=("v1",)).passed
    assert not grade_case({"expected_files": ["bug.py"], "required_check": True}, changed_files=("bug.py",), outcome="unverified", verification_ids=()).passed


def test_swebench_predictions_use_the_official_three_field_shape(tmp_path):
    prediction = Prediction(
        instance_id="ion__local-bug-fix",
        model_name_or_path="ion-test-model",
        model_patch="diff --git a/bug.py b/bug.py\n",
    )
    path = tmp_path / "predictions.jsonl"
    serialize_predictions(path, [prediction])
    assert load_predictions(path) == (prediction,)
    assert json.loads(path.read_text()) == {
        "instance_id": "ion__local-bug-fix",
        "model_name_or_path": "ion-test-model",
        "model_patch": "diff --git a/bug.py b/bug.py\n",
    }


def test_swebench_predictions_reject_duplicate_or_malformed_records(tmp_path):
    path = tmp_path / "predictions.jsonl"
    path.write_text(json.dumps({"instance_id": "same", "model_name_or_path": "m", "model_patch": ""}) + "\n" +
                    json.dumps({"instance_id": "same", "model_name_or_path": "m", "model_patch": ""}) + "\n")
    with pytest.raises(ValueError, match="duplicate"):
        load_predictions(path)

    path.write_text(json.dumps({"instance_id": "missing-patch", "model_name_or_path": "m"}) + "\n")
    with pytest.raises(ValueError, match="model_patch"):
        load_predictions(path)


def test_local_swebench_check_runs_a_real_test_command(tmp_path):
    (tmp_path / "test_value.py").write_text("def test_value():\n    assert 1 + 1 == 2\n")
    check = run_local_check([sys.executable, "-m", "pytest", "test_value.py", "-q"], cwd=tmp_path)
    assert check.passed
    assert check.returncode == 0
    graded = evaluate_local_case(
        {"id": "local", "expected_files": ("test_value.py",), "required_check": True,
         "test_command": [sys.executable, "-m", "pytest", "test_value.py", "-q"]},
        cwd=tmp_path, changed_files=("test_value.py",), outcome="verified",
    )
    assert graded["status"] == "passed"
