import pytest

from ion.budget import BudgetError, BudgetLedger, BudgetFailureCode
from ion.contracts import BudgetReport, Outcome, Phase, TaskResult


def test_physical_request_reservations_settle_independently():
    ledger = BudgetLedger(max_requests=4, max_total_tokens=1000, reserve_verification=False)
    first = ledger.reserve(Phase.act, 10, 20)
    second = ledger.reserve(Phase.act, 30, 40)
    assert first.attempt == 1 and second.attempt == 2
    ledger.settle(12, 8, first.attempt)
    assert ledger.tokens_used == 90
    ledger.settle(20, 10, second.attempt)
    assert ledger.tokens_used == 50


def test_verification_reserve_blocks_discretionary_work():
    ledger = BudgetLedger(max_requests=5, reserve_verification=True)
    for _ in range(4):
        ledger.admit(Phase.act)
    with pytest.raises(RuntimeError, match="verification reserve"):
        ledger.admit(Phase.act)
    assert ledger.admit(Phase.verify).attempt == 5


def test_snapshot_separates_estimates_from_settled_usage():
    ledger = BudgetLedger(max_requests=3, max_total_tokens=1000, reserve_verification=False)
    first = ledger.reserve(Phase.act, 100, 200)
    second = ledger.reserve(Phase.act, 50, 100)
    ledger.settle(80, 20, first.attempt)

    snapshot = ledger.snapshot()
    assert snapshot.requests_used == 2
    assert snapshot.requests_remaining == 1
    assert snapshot.settled_tokens == 100
    assert snapshot.reserved_tokens == 150
    assert snapshot.remaining_tokens == 750
    assert snapshot.estimated_attempts == (second.attempt,)
    assert snapshot.deadline_reached is False


def test_unknown_usage_remains_unsettled():
    ledger = BudgetLedger(max_requests=2, max_total_tokens=1000, reserve_verification=False)
    reservation = ledger.reserve(Phase.act, 100, 200)
    ledger.settle(100, None, reservation.attempt)

    snapshot = ledger.snapshot()
    assert snapshot.estimated_attempts == (reservation.attempt,)
    assert snapshot.reserved_tokens == 300
    assert snapshot.settled_tokens == 0
    assert ledger.tokens_used == 300


def test_http_rejection_settles_zero_usage_and_keeps_attempt():
    ledger = BudgetLedger(max_requests=2, max_total_tokens=500, reserve_verification=False)
    reservation = ledger.reserve(Phase.act, 100, 200)
    ledger.settle(attempt=reservation.attempt, model_tokens=0)

    snapshot = ledger.snapshot()
    assert snapshot.requests_used == 1
    assert snapshot.requests_remaining == 1
    assert snapshot.reserved_tokens == 0
    assert snapshot.settled_tokens == 0
    assert snapshot.estimated_attempts == ()
    assert ledger.tokens_used == 0


def test_protected_token_admission_is_typed_and_atomic():
    ledger = BudgetLedger(max_requests=2, max_total_tokens=1000, reserve_verification=False)
    with pytest.raises(BudgetError) as exc:
        ledger.reserve(Phase.act, 400, 300, protected_tokens=301)
    assert exc.value.code == BudgetFailureCode.token_limit
    assert exc.value.dispatched is False
    assert ledger.snapshot().requests_used == 0
    assert ledger.reserve(Phase.act, 400, 300, protected_tokens=300).attempt == 1


def test_request_and_deadline_failures_are_typed():
    ledger = BudgetLedger(max_requests=1, reserve_verification=False)
    ledger.reserve(Phase.act)
    with pytest.raises(BudgetError) as exc:
        ledger.reserve(Phase.act)
    assert exc.value.code == BudgetFailureCode.request_limit
    assert exc.value.dispatched is False

    expired = BudgetLedger(deadline_seconds=-1)
    with pytest.raises(BudgetError) as exc:
        expired.reserve(Phase.act)
    assert exc.value.code == BudgetFailureCode.deadline
    assert expired.snapshot().deadline_reached is True


def test_task_result_budget_fields_are_backward_compatible_and_strict():
    legacy = TaskResult(task_id="old", outcome=Outcome.unverified, summary="pending")
    assert legacy.error_category is None
    assert legacy.request_dispatched is None
    assert legacy.budget is None
    report = BudgetReport(requests_used=1, requests_remaining=2, settled_tokens=10,
                          reserved_tokens=20, token_limit=100, deadline_reached=False)
    result = TaskResult(task_id="new", outcome=Outcome.budget_exhausted, summary="limit",
                        error_category="token_limit", request_dispatched=False, budget=report)
    assert result.budget.reserved_tokens == 20
    with pytest.raises(ValueError):
        BudgetReport(requests_used=1, requests_remaining=2, settled_tokens=10,
                     reserved_tokens=20, token_limit=100, deadline_reached=False, extra=1)
