from __future__ import annotations

from collections import deque
from typing import AsyncIterator, Sequence

from ion.contracts import ModelEvent, ModelRequest


class ScriptedProvider:
    def __init__(self, turns: Sequence[Sequence[ModelEvent]]) -> None:
        self.turns = deque(tuple(turn) for turn in turns)
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        if not self.turns:
            yield ModelEvent(kind="error", error="scripted provider exhausted")
            return
        for event in self.turns.popleft():
            yield event
