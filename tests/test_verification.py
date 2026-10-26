from ion.verification import observe_command, observe_pytest
from ion.contracts import VerificationEvidence, VerificationRecord
from ion.verification import CompletionGate
from ion.workspace import ChangeSet

fingerprint = "workspace-hash"


def test_relevant_pytest_output_is_observed_but_echo_is_not():
    fingerprint = "workspace-hash"
    changed = ("bug.py",)
    assert observe_pytest("python -m pytest tests/test_bug.py -q", "1 passed in 0.01s", 0, "op", fingerprint, changed)
    assert observe_pytest("echo '1 passed' && pytest tests/test_bug.py", "1 passed in 0.01s", 0, "op", fingerprint, changed) is None
    assert observe_pytest("python -m pytest tests/test_unrelated.py -q", "1 passed in 0.01s", 0, "op", fingerprint, changed) is None


def test_common_non_pytest_runners_require_success_markers_and_changed_scope():
    assert observe_command(
        "cargo test", "test result: ok. 2 passed; 0 failed", 0, "cargo", fingerprint, ("src/lib.rs",)
    )
    assert observe_command("go test ./...", "ok\texample/pkg\t0.01s", 0, "go", fingerprint, ("pkg/value.go",))
    assert observe_command("npm test", "Tests: 1 passed, 1 total", 0, "npm", fingerprint, ("src/value.js",))
    assert observe_command("npm test", "Tests: 0 passed, 0 total", 0, "empty", fingerprint, ("src/value.js",)) is None
    assert observe_command("cargo test", "test result: ok. 2 passed", 0, "chain", fingerprint, ()) is None


def test_static_evidence_can_verify_a_noop_inspection_task():
    record = VerificationRecord(
        criterion_ids=("inspect",),
        evidence=VerificationEvidence(kind="static", source_refs=("README.md:1-2",)),
        workspace_fingerprint=fingerprint,
        status="passed",
    )
    changes = ChangeSet((), (), (), ())
    assert CompletionGate().decide(changes, [record], fingerprint) == "verified"
