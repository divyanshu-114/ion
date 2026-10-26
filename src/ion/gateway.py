from __future__ import annotations

import hashlib
import json
from typing import AsyncIterator, Protocol

from ion.contracts import ModelEvent, ModelProfile, ModelRequest


class ModelGateway(Protocol):
    def generate(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...


def profile_digest(profile: ModelProfile) -> str:
    raw = json.dumps(profile.model_dump(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()
