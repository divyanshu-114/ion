from ion.contracts import EngineEvent, Outcome, Phase, TaskResult, TaskSpec
from ion.storage import RunStore


def test_run_history_survives_reopen_and_records_interruption(tmp_path):
    path = tmp_path / "data" / "runs.sqlite3"
    task = TaskSpec(text="Fix the parser", repo_path=str(tmp_path), profile_name="groq-qwen-dev")
    store = RunStore(path)
    assert path.stat().st_mode & 0o077 == 0
    store.begin(task)
    store.append(task.task_id, EngineEvent(phase=Phase.inspect, message="Read parser.py"))
    store.finish(TaskResult(task_id=task.task_id, outcome=Outcome.unverified, summary="Needs broader checks"))
    store.close()

    reopened = RunStore(path)
    rows = reopened.recent()
    assert len(rows) == 1
    assert rows[0]["task"]["text"] == "Fix the parser"
    assert rows[0]["result"]["summary"] == "Needs broader checks"
    assert rows[0]["status"] == "unverified"
    detail = reopened.inspect(task.task_id)
    assert detail and detail["events"] == [{"phase": "inspect", "message": "Read parser.py"}]
    assert reopened.inspect("missing") is None
    reopened.interrupt(task.task_id)
    assert reopened.recent()[0]["status"] == "unverified"
    reopened.close()


def test_operation_intent_survives_crash_until_settled(tmp_path):
    path = tmp_path / "data" / "runs.sqlite3"
    task = TaskSpec(text="Fix parser", repo_path=str(tmp_path), profile_name="groq-qwen-dev")
    store = RunStore(path)
    store.begin(task)
    assert store.prepare_operation(task.task_id, "op-1", "command_start", {"command": "pytest"})
    assert store.unresolved_operations(task.task_id)[0]["operation_id"] == "op-1"
    store.close()

    reopened = RunStore(path)
    assert reopened.unresolved_operations(task.task_id)[0]["status"] == "prepared"
    reopened.settle_operation(task.task_id, "op-1", {"status": "unknown"}, "unknown")
    assert reopened.unresolved_operations(task.task_id)[0]["status"] == "unknown"
    reopened.settle_operation(task.task_id, "op-1", {"status": "reconciled", "exit_code": 0}, "succeeded")
    assert reopened.unresolved_operations(task.task_id) == []
    assert reopened.operation(task.task_id, "op-1")["status"] == "succeeded"
    reopened.close()


def test_duplicate_operation_intent_is_idempotent(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3")
    task = TaskSpec(text="Fix parser", repo_path=str(tmp_path), profile_name="groq-qwen-dev")
    store.begin(task)
    assert store.prepare_operation(task.task_id, "op", "file_read", {"path": "a.py"})
    assert not store.prepare_operation(task.task_id, "op", "file_read", {"path": "a.py"})
    store.close()
