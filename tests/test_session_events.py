from ion.contracts import EngineEvent, Outcome, Phase, TaskResult, TaskSpec
from ion.session import SessionService
from ion.storage import RunStore


def test_reconnect_replays_ordered_events_without_gaps(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3")
    service = SessionService(store)
    task = TaskSpec(text="Inspect parser", repo_path=str(tmp_path), profile_name="groq-qwen-dev")
    session_id = service.start(task)
    assert service.append(session_id, EngineEvent(phase=Phase.intake, message="accepted")) == 1
    assert service.append(session_id, EngineEvent(phase=Phase.inspect, message="read")) == 2
    replay = service.subscribe(session_id, after_seq=0)
    assert [item["seq"] for item in replay] == [1, 2]
    assert service.subscribe(session_id, after_seq=1)[0]["seq"] == 2
    store.close()


def test_mutating_request_is_deduplicated(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3")
    service = SessionService(store)
    task = TaskSpec(text="Inspect parser", repo_path=str(tmp_path), profile_name="groq-qwen-dev")
    session_id = service.start(task)
    first = service.control(session_id, "pause", "request-1")
    second = service.control(session_id, "pause", "request-1")
    assert first == second
    assert len(service.subscribe(session_id)) == 1
    store.close()


def test_session_controls_reject_finished_and_missing_runs(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3")
    service = SessionService(store)
    task = TaskSpec(text="Inspect parser", repo_path=str(tmp_path), profile_name="groq-qwen-dev")
    session_id = service.start(task)
    store.finish(TaskResult(task_id=session_id, outcome=Outcome.verified, summary="done"))
    assert service.control(session_id, "cancel", "cancel-finished")["ok"] is False
    missing = service.control("missing", "cancel", "cancel-missing")
    assert missing == {"ok": False, "error": "session not found"}
    store.close()
