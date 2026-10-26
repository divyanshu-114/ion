from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from time import monotonic

import httpx

from ion.contracts import ModelInfo, ModelProfile


# Published model limits, checked against provider docs on 2026-09-26.
# Entries only enrich IDs returned by the live provider catalog.
GROQ_FREE_PLAN = {
    "qwen/qwen3.8-27b": (131072, 16384),
}


@dataclass(frozen=True)
class CatalogResult:
    entries: tuple[ModelInfo, ...]
    status: str
    detail: str = ""


@dataclass(frozen=True)
class QuotaResult:
    status: str
    remaining: int | None = None
    limit: int | None = None
    detail: str = ""


class ModelCatalog:
    def __init__(self, credential: str, transport: httpx.AsyncBaseTransport | None = None, cache_ttl_seconds: int = 300) -> None:
        self.credential = credential
        self.transport = transport
        self.cache_ttl_seconds = cache_ttl_seconds
        self.cache: dict[tuple, tuple[float, CatalogResult]] = {}
        self.quota_cache: dict[str, tuple[float, QuotaResult]] = {}

    async def list(self, profile: ModelProfile, refresh: bool = False) -> CatalogResult:
        key = (profile.endpoint, profile.provider, profile.model_id, profile.tool_protocol,
               profile.context_window, profile.max_output_tokens)
        if not refresh and key in self.cache and monotonic() - self.cache[key][0] < self.cache_ttl_seconds:
            return self.cache[key][1]
        try:
            async with httpx.AsyncClient(base_url=profile.endpoint.rstrip("/") + "/", transport=self.transport, follow_redirects=False, timeout=10) as client:
                response = await client.get("models", headers={"Authorization": f"Bearer {self.credential}"})
            if response.status_code == 401:
                return CatalogResult((), "authentication_failed", "API key rejected")
            if response.status_code == 403:
                return CatalogResult((), "access_denied", "API key lacks catalog access")
            if response.status_code == 429:
                return CatalogResult((), "rate_limited", "Model catalog rate limited")
            if response.status_code >= 400:
                return CatalogResult((), "provider_error", f"Model catalog returned HTTP {response.status_code}")
            payload = response.json()
            entries = tuple(self._parse(profile, item) for item in payload["data"] if isinstance(item, dict) and isinstance(item.get("id"), str))
            result = CatalogResult(entries, "available" if entries else "empty", "" if entries else "Provider returned an empty model catalog")
        except httpx.TimeoutException:
            result = CatalogResult((), "timeout", "Model catalog request timed out")
        except httpx.ConnectError:
            result = CatalogResult((), "connection_failed", "Could not connect to the model catalog")
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            result = CatalogResult((), "invalid_response", "Provider returned an invalid model catalog")
        if result.status == "available":
            self.cache[key] = (monotonic(), result)
        return result

    async def quota(self, profile: ModelProfile, refresh: bool = False) -> QuotaResult:
        if profile.provider != "openrouter":
            return QuotaResult("unsupported", detail="Provider does not expose a compatible quota endpoint")
        key = profile.endpoint
        if not refresh and key in self.quota_cache and monotonic() - self.quota_cache[key][0] < min(self.cache_ttl_seconds, 60):
            return self.quota_cache[key][1]
        try:
            async with httpx.AsyncClient(base_url=key.rstrip("/") + "/", transport=self.transport, follow_redirects=False, timeout=10) as client:
                response = await client.get("key", headers={"Authorization": f"Bearer {self.credential}"})
            if response.status_code == 401:
                return QuotaResult("authentication_failed", detail="API key rejected")
            if response.status_code != 200:
                return QuotaResult("unavailable", detail=f"Quota endpoint returned HTTP {response.status_code}")
            data = response.json().get("data", {})
            free = data.get("free_model_daily_requests") or {}
            remaining, limit = free.get("remaining"), free.get("limit")
            if not isinstance(remaining, int) or not isinstance(limit, int):
                return QuotaResult("unavailable", detail="Quota response omitted free-model limits")
            result = QuotaResult("available", remaining=remaining, limit=limit)
            self.quota_cache[key] = (monotonic(), result)
            return result
        except httpx.TimeoutException:
            return QuotaResult("timeout", detail="Quota request timed out")
        except httpx.ConnectError:
            return QuotaResult("connection_failed", detail="Could not connect to the quota endpoint")
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            return QuotaResult("invalid_response", detail="Provider returned an invalid quota response")

    def _parse(self, profile: ModelProfile, item: dict) -> ModelInfo:
        model_id = item["id"]
        if profile.provider == "groq":
            limits = GROQ_FREE_PLAN.get(model_id)
            context_window = limits[0] if limits else item.get("context_window")
            return ModelInfo(
                model_id=model_id,
                context_window=context_window,
                max_output_tokens=limits[1] if limits else profile.max_output_tokens,
                text_only=bool(context_window and context_window > 1024 and not any(term in model_id for term in ("whisper", "orpheus", "guard"))),
                supports_tools=True if model_id == "qwen/qwen3.8-27b" else None,
                free=None,
                available=item.get("active", True) and bool(context_window),
            )
        architecture = item.get("architecture") or {}
        params = item.get("supported_parameters") or []
        pricing = item.get("pricing") or {}
        top = item.get("top_provider") or {}
        free = None
        if profile.provider == "openrouter":
            try:
                free = Decimal(str(pricing["prompt"])) == 0 and Decimal(str(pricing["completion"])) == 0
            except (InvalidOperation, KeyError, TypeError):
                free = None
        is_configured = model_id == profile.model_id
        text_usable = (
            "text" in architecture.get("input_modalities", []) and "text" in architecture.get("output_modalities", [])
            if architecture else is_configured
        )
        context_window = item.get("context_length") or item.get("context_window") or (profile.context_window if is_configured else None)
        return ModelInfo(
            model_id=model_id,
            context_window=context_window,
            max_output_tokens=top.get("max_completion_tokens") or profile.max_output_tokens,
            text_only=text_usable,
            supports_tools="tools" in params if params else (profile.tool_protocol == "native" if is_configured else None),
            free=free,
            available=text_usable and bool(context_window),
        )
