import pytest

from ion.budget import BudgetError, BudgetLedger
from ion.budget_policy import BudgetPolicy, WorkClass
from ion.contracts import ModelProfile


@pytest.fixture
def profile():
    return ModelProfile(provider="test", endpoint="https://example.invalid", model_id="model",
                        context_window=8192, max_output_tokens=4096)


def test_small_action_gets_reduced_cap_when_profile_max_would_not_fit(profile):
    ledger = BudgetLedger(max_total_tokens=1800)
    snapshot = ledger.snapshot()
    plan = BudgetPolicy().plan(WorkClass.edit, input_tokens=700, profile=profile, snapshot=snapshot)

    assert plan.minimum_output_tokens == 384
    assert plan.output_cap == 588
    assert plan.output_cap <= 1024
    assert plan.protected_tokens == 512
    assert plan.reserved_total_tokens == 1288
    assert ledger.reserve("act", plan.input_tokens, plan.output_cap,
                          protected_tokens=plan.protected_tokens).attempt == 1


def test_minimum_output_failure_is_typed(profile):
    snapshot = BudgetLedger(max_total_tokens=1500).snapshot()
    with pytest.raises(BudgetError) as exc:
        BudgetPolicy().plan(WorkClass.edit, input_tokens=700, profile=profile, snapshot=snapshot)

    assert exc.value.code == "token_limit"
    assert exc.value.dispatched is False
    assert exc.value.details["input_tokens"] == 700
    assert exc.value.details["minimum_output_tokens"] == 384
    assert exc.value.details["remaining_tokens"] == 1500


@pytest.mark.parametrize(("work_class", "minimum", "preferred", "protected"), [
    (WorkClass.inspect, 256, 512, 512),
    (WorkClass.edit, 384, 1024, 512),
    (WorkClass.rewrite, 768, 4096, 512),
    (WorkClass.verify, 256, 512, 256),
    (WorkClass.finalize, 256, 512, 0),
])
def test_output_tiers_and_phase_reserves(profile, work_class, minimum, preferred, protected):
    plan = BudgetPolicy().plan(work_class, input_tokens=100, profile=profile,
                               snapshot=BudgetLedger(max_total_tokens=10000).snapshot())
    assert (plan.minimum_output_tokens, plan.output_cap, plan.protected_tokens) == (minimum, preferred, protected)


def test_policy_uses_existing_reserved_and_settled_usage(profile):
    ledger = BudgetLedger(max_total_tokens=2000, reserve_verification=False)
    first = ledger.reserve(phase="act", input_tokens=400, output_tokens=200)
    ledger.settle(100, 100, first.attempt)
    ledger.reserve(phase="act", input_tokens=100, output_tokens=200)

    plan = BudgetPolicy().plan(WorkClass.edit, input_tokens=500, profile=profile, snapshot=ledger.snapshot())
    assert plan.output_cap == 488


def test_policy_rejects_when_profile_cannot_supply_minimum(profile):
    limited = profile.model_copy(update={"max_output_tokens": 256})
    with pytest.raises(BudgetError, match="minimum") as exc:
        BudgetPolicy().plan(WorkClass.edit, input_tokens=100, profile=limited,
                            snapshot=BudgetLedger(max_total_tokens=10000).snapshot())
    assert exc.value.code == "token_limit"
