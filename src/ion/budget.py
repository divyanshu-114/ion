from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from threading import Lock
from time import monotonic
from typing import Any

from ion.contracts import Phase


@dataclass(frozen=True)
class RequestReservation:
    attempt: int
    remaining: int


class BudgetFailureCode(StrEnum):
    request_limit = "request_limit"
    verification_reserve = "verification_reserve"
    token_limit = "token_limit"
    context_overflow = "context_overflow"
    latest_turn_overflow = "latest_turn_overflow"
    deadline = "deadline"


class BudgetError(RuntimeError):
    def __init__(self, code: BudgetFailureCode, message: str, *,
                 dispatched: bool = False, details: dict[str, Any] | None = None,
                 suggested_action: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.dispatched = dispatched
        self.details = details or {}
        self.suggested_action = suggested_action


class UsageState(StrEnum):
    estimated = "estimated"
    settled = "settled"


@dataclass(frozen=True)
class AttemptUsage:
    attempt: int
    state: UsageState
    reserved_tokens: int
    model_tokens: int | None


@dataclass(frozen=True)
class BudgetSnapshot:
    requests_used: int
    requests_remaining: int
    settled_tokens: int
    reserved_tokens: int
    token_limit: int | None
    deadline_reached: bool
    attempts: tuple[AttemptUsage, ...] = ()

    @property
    def remaining_tokens(self) -> int | None:
        if self.token_limit is None:
            return None
        return self.token_limit - self.settled_tokens - self.reserved_tokens

    @property
    def estimated_attempts(self) -> tuple[int, ...]:
        return tuple(item.attempt for item in self.attempts if item.state is UsageState.estimated)


class BudgetLedger:
    def __init__(self, max_requests: int = 24, deadline_seconds: float = 600, max_total_tokens: int | None = None, reserve_verification: bool = True) -> None:
        if max_requests < 1:
            raise ValueError("max_requests must be positive")
        if not isfinite(deadline_seconds):
            raise ValueError("deadline_seconds must be finite")
        if max_total_tokens is not None and max_total_tokens < 0:
            raise ValueError("max_total_tokens cannot be negative")
        self.max_requests = max_requests
        self.deadline = monotonic() + deadline_seconds
        self.used = 0
        self.max_total_tokens = max_total_tokens
        self.tokens_used = 0
        self._settled_tokens = 0
        self.reserve_verification = reserve_verification
        self._reservations: dict[int, int] = {}
        self._settlements: dict[int, int] = {}
        self._lock = Lock()

    def snapshot(self) -> BudgetSnapshot:
        with self._lock:
            attempts = tuple(
                AttemptUsage(attempt, UsageState.estimated, self._reservations[attempt], None)
                if attempt in self._reservations else
                AttemptUsage(attempt, UsageState.settled, 0, self._settlements[attempt])
                for attempt in range(1, self.used + 1)
            )
            return BudgetSnapshot(
                requests_used=self.used,
                requests_remaining=self.max_requests - self.used,
                settled_tokens=self._settled_tokens,
                reserved_tokens=sum(self._reservations.values()),
                token_limit=self.max_total_tokens,
                deadline_reached=monotonic() >= self.deadline,
                attempts=attempts,
            )

    def admit(self, phase: Phase, input_tokens: int = 0, output_tokens: int = 0,
              *, protected_tokens: int = 0) -> RequestReservation:
        if protected_tokens < 0:
            raise ValueError("protected_tokens cannot be negative")
        with self._lock:
            if monotonic() >= self.deadline:
                raise BudgetError(BudgetFailureCode.deadline, "budget deadline reached",
                                  details={"requests_remaining": self.max_requests - self.used},
                                  suggested_action="Start a new task if more time is needed.")
            if self.used >= self.max_requests:
                raise BudgetError(BudgetFailureCode.request_limit, "request budget exhausted",
                                  details={"requests_remaining": 0, "request_limit": self.max_requests},
                                  suggested_action="Start a new task with a larger request limit.")
            if self.reserve_verification and self.used >= int(self.max_requests * 0.8) and phase not in (Phase.verify, Phase.finalize):
                raise BudgetError(BudgetFailureCode.verification_reserve, "verification reserve reached",
                                  details={"requests_remaining": self.max_requests - self.used},
                                  suggested_action="Use the remaining requests for verification or finalization.")
            reservation = max(0, input_tokens) + max(0, output_tokens)
            if self.max_total_tokens is not None and self.tokens_used + reservation + protected_tokens > self.max_total_tokens:
                raise BudgetError(BudgetFailureCode.token_limit, "total token budget exhausted",
                                  details={"input_tokens": input_tokens, "output_tokens": output_tokens,
                                           "protected_tokens": protected_tokens,
                                           "remaining_tokens": self.max_total_tokens - self.tokens_used,
                                           "token_limit": self.max_total_tokens},
                                  suggested_action="Reduce input or output before retrying.")
            self.used += 1
            self._reservations[self.used] = reservation
            self.tokens_used += reservation
            return RequestReservation(self.used, self.max_requests - self.used)

    reserve = admit

    def settle(self, input_tokens: int | None = None, output_tokens: int | None = None,
               attempt: int | None = None, *, model_tokens: int | None = None) -> None:
        """Replace the estimate only when the provider reports complete usage."""
        with self._lock:
            if attempt is None:
                attempt = max(self._reservations, default=0)
            if model_tokens is None:
                if input_tokens is None or output_tokens is None:
                    return
                model_tokens = max(0, input_tokens) + max(0, output_tokens)
            elif input_tokens is not None or output_tokens is not None:
                raise ValueError("pass either model_tokens or complete input/output usage")
            if model_tokens < 0:
                raise ValueError("model_tokens cannot be negative")
            if attempt not in self._reservations:
                raise ValueError(f"unknown or already settled request attempt: {attempt}")
            estimate = self._reservations.pop(attempt)
            self._settlements[attempt] = model_tokens
            self._settled_tokens += model_tokens
            self.tokens_used += model_tokens - estimate
