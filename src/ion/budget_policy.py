from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ion.budget import BudgetError, BudgetFailureCode, BudgetSnapshot
from ion.contracts import ModelProfile


class WorkClass(StrEnum):
    inspect = "inspect"
    edit = "edit"
    rewrite = "rewrite"
    verify = "verify"
    finalize = "finalize"


@dataclass(frozen=True)
class OutputPlan:
    work_class: WorkClass
    input_tokens: int
    minimum_output_tokens: int
    preferred_output_tokens: int
    output_cap: int
    protected_tokens: int
    reserved_total_tokens: int
    remaining_tokens_before: int | None
    remaining_tokens_after: int | None


class BudgetPolicy:
    def __init__(self, verification_tokens: int = 256, finalization_tokens: int = 256,
                 preferred_output_tokens: dict[WorkClass, int] | None = None) -> None:
        if verification_tokens < 0 or finalization_tokens < 0:
            raise ValueError("protected output reserves cannot be negative")
        self.verification_tokens = verification_tokens
        self.finalization_tokens = finalization_tokens
        self.preferred_output_tokens = preferred_output_tokens or {}

    def plan(self, work_class: WorkClass, input_tokens: int, profile: ModelProfile,
             snapshot: BudgetSnapshot) -> OutputPlan:
        if input_tokens < 0:
            raise ValueError("input_tokens cannot be negative")
        if snapshot.deadline_reached:
            raise BudgetError(BudgetFailureCode.deadline, "budget deadline reached",
                              details={"requests_remaining": snapshot.requests_remaining})
        if snapshot.requests_remaining <= 0:
            raise BudgetError(BudgetFailureCode.request_limit, "request budget exhausted",
                              details={"requests_remaining": snapshot.requests_remaining})

        minimum, preferred = {
            WorkClass.inspect: (256, 512),
            WorkClass.edit: (384, 1024),
            WorkClass.rewrite: (768, profile.max_output_tokens),
            WorkClass.verify: (256, 512),
            WorkClass.finalize: (256, 512),
        }[work_class]
        preferred = self.preferred_output_tokens.get(work_class, preferred)
        if preferred < minimum:
            raise ValueError("preferred output tokens cannot be below the work-class minimum")
        preferred = min(preferred, profile.max_output_tokens)
        if work_class == WorkClass.finalize:
            protected = 0
        elif work_class == WorkClass.verify:
            protected = self.finalization_tokens
        else:
            protected = self.verification_tokens + self.finalization_tokens
        remaining = snapshot.remaining_tokens
        cap = preferred if remaining is None else min(preferred, remaining - input_tokens - protected)
        if cap < minimum:
            raise BudgetError(BudgetFailureCode.token_limit, "minimum output allowance cannot fit",
                              details={"input_tokens": input_tokens, "minimum_output_tokens": minimum,
                                       "remaining_tokens": remaining, "protected_tokens": protected,
                                       "profile_max_output_tokens": profile.max_output_tokens},
                              suggested_action="Reduce input or use a model with a larger output limit.")
        reserved = input_tokens + cap
        return OutputPlan(work_class=work_class, input_tokens=input_tokens,
                          minimum_output_tokens=minimum, preferred_output_tokens=preferred,
                          output_cap=cap, protected_tokens=protected,
                          reserved_total_tokens=reserved, remaining_tokens_before=remaining,
                          remaining_tokens_after=None if remaining is None else remaining - reserved)
