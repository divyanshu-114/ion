from __future__ import annotations

import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from math import isfinite
from typing import AsyncIterator

import httpx

from ion.contracts import ModelEvent, ModelProfile, ModelRequest


class OpenAICompatibleProvider:
    def __init__(
        self,
        profile: ModelProfile,
        credential: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.profile = profile
        self.credential = credential
        self.transport = transport

    @staticmethod
    def _retry_after(value: str | None) -> float | None:
        if not value:
            return None
        try:
            seconds = float(value)
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                seconds = (when - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                return None
        return seconds if isfinite(seconds) and seconds >= 0 else None

    @staticmethod
    def _rejected(error: str, retry_after_seconds: float | None = None) -> ModelEvent:
        # A non-streaming HTTP rejection did not produce model usage. Supplying
        # zero usage lets the controller release its admission estimate.
        return ModelEvent(
            kind="error",
            error=error,
            retry_after_seconds=retry_after_seconds,
            usage={"prompt_tokens": 0, "completion_tokens": 0},
        )

    async def generate(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        body: dict = {
            "model": self.profile.model_id,
            "messages": list(request.messages),
            "max_tokens": request.max_output_tokens,
            "stream": False,
        }
        if self.profile.provider == "deepseek":
            # Thinking-mode tool turns require replaying reasoning_content.
            # Ion retains tool evidence, not provider reasoning, in its history.
            body["thinking"] = {"type": "disabled"}
        if self.profile.tool_protocol == "native" and request.tools:
            body["tools"] = list(request.tools)
            body["tool_choice"] = ({"type": "function", "function": {"name": request.tool_choice}}
                                   if request.tool_choice else "auto")
        try:
            async with httpx.AsyncClient(
                base_url=self.profile.endpoint.rstrip("/") + "/",
                transport=self.transport,
                follow_redirects=False,
                timeout=60,
            ) as client:
                response = await client.post(
                    "chat/completions",
                    headers={"Authorization": f"Bearer {self.credential}"},
                    json=body,
                )
            if response.status_code == 401:
                yield self._rejected("provider authentication failed")
                return
            if response.status_code == 402:
                yield self._rejected("provider credits exhausted")
                return
            if response.status_code == 403:
                yield self._rejected("provider access denied (403)")
                return
            if response.status_code == 404:
                yield self._rejected(f"model unavailable: {self.profile.model_id}")
                return
            if response.status_code == 429:
                retry_after = self._retry_after(response.headers.get("retry-after"))
                remaining = response.headers.get("x-ratelimit-remaining")
                try:
                    quota_exhausted = remaining is not None and float(remaining) <= 0
                except ValueError:
                    quota_exhausted = False
                error = "provider quota exhausted" if quota_exhausted else "provider rate limit"
                yield self._rejected(error, retry_after)
                return
            if response.status_code >= 500:
                yield self._rejected(f"provider server error ({response.status_code})")
                return
            if response.status_code >= 400:
                yield self._rejected(f"provider rejected request ({response.status_code})")
                return
            payload = response.json()
            choice = payload["choices"][0]
            usage = payload.get("usage")
            if isinstance(usage, dict):
                counts = {key: value for key, value in usage.items() if isinstance(value, int)}
                details = usage.get("completion_tokens_details") or {}
                if isinstance(details, dict) and isinstance(details.get("reasoning_tokens"), int):
                    counts["reasoning_tokens"] = details["reasoning_tokens"]
                yield ModelEvent(kind="usage", usage=counts)
            finish_reason = choice.get("finish_reason")
            if finish_reason == "length":
                # Never execute any part of a truncated response, even if one
                # earlier tool call happens to contain valid JSON.
                yield ModelEvent(kind="error", error="provider output truncated", finish_reason="length")
                return
            message = choice["message"]
            content = message.get("content")
            if isinstance(content, str) and content:
                yield ModelEvent(kind="text_delta", text=content)
            for call in message.get("tool_calls") or []:
                function = call["function"]
                arguments = json.loads(function["arguments"])
                if not isinstance(arguments, dict):
                    raise ValueError("tool arguments must be an object")
                yield ModelEvent(kind="tool_call", tool=function["name"], arguments=arguments, call_id=call["id"])
            yield ModelEvent(kind="completed", finish_reason=finish_reason)
        except httpx.TimeoutException:
            yield ModelEvent(kind="error", error="provider request timed out")
        except httpx.ConnectError:
            yield ModelEvent(kind="error", error="provider connection failed")
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            # Provider bodies can contain credentials; never retain raw response text.
            category = "provider request failed" if isinstance(exc, httpx.HTTPError) else "malformed provider response"
            yield ModelEvent(kind="error", error=category)
