import json

import pytest

from ion.compaction import Compactor


SUMMARY = """Objective: fix parser\nConstraints: do not change dependencies\nCompleted: read parser.py\nActive: patch parser\nBlockers: none\nNext actions: run tests\n"""


def test_compaction_commits_atomic_checkpoint_with_latest_constraints(tmp_path):
    compactor = Compactor(tmp_path / "checkpoints")
    checkpoint = compactor.compact(
        "session", 4, SUMMARY, task_text="Fix parser", constraints=("do not change dependencies",), amendment_version=2,
        model_profile_digest="profile-a", recent_event_refs=("event-4",), pinned_evidence_refs=("read-1",),
    )
    assert checkpoint.through_seq == 4
    assert checkpoint.constraints_digest
    loaded = compactor.load("session")
    assert loaded and "do not change dependencies" in loaded.summary
    assert json.loads((tmp_path / "checkpoints" / "session.json").read_text())["amendment_version"] == 2


def test_invalid_summary_does_not_replace_previous_checkpoint(tmp_path):
    compactor = Compactor(tmp_path / "checkpoints")
    compactor.compact("session", 1, SUMMARY, task_text="Fix parser", constraints=(), amendment_version=1, model_profile_digest="p")
    with pytest.raises(ValueError, match="required section"):
        compactor.compact("session", 2, "Objective: missing sections", task_text="Fix parser", constraints=(), amendment_version=2, model_profile_digest="p")
    assert compactor.load("session").through_seq == 1


def test_compaction_bounds_pinned_task_and_summary_together(tmp_path):
    compactor = Compactor(tmp_path / "checkpoints")
    with pytest.raises(ValueError, match="bounded size"):
        compactor.compact(
            "session", 1, SUMMARY, task_text="x" * 12000, constraints=(),
            model_profile_digest="p",
        )
