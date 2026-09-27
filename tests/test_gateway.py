import json
from pathlib import Path

import httpx
import pytest

from ion.config import load_config, resolve_credential, resolve_profile
from ion.contracts import ModelRequest
from ion.providers.openai_compatible import OpenAICompatibleProvider


@pytest.mark.parametrize("provider,endpoint", [
    ("deepseek", "https://api.deepseek.com"),
    ("qwen", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    ("custom", "https://judge.example/v1"),
])
async def test_universal_key_reaches_only_selected_endpoint(monkeypatch, provider, endpoint):
    monkeypatch.setenv("AI_PROVIDER", provider)
    monkeypatch.setenv("AI_BASE_URL", endpoint)
    monkeypatch.setenv("AI_MODEL", "judge-model")
    monkeypatch.setenv("AI_API_KEY", "judge-key")
    monkeypatch.setenv("AI_EVALUATION", "1")
    config = load_config(Path(__file__).resolve().parents[1] / "ion.toml")
    profile = resolve_profile(config, config.evaluation_profile, "evaluation")
    credential, _ = resolve_credential(profile, "evaluation")

    def handle(request):
        assert str(request.url) == endpoint + "/chat/completions"
        assert request.headers["Authorization"] == "Bearer judge-key"
        assert json.loads(request.content)["model"] == "judge-model"
        return httpx.Response(200, json={"choices": [{"message": {"content": "Ready"}}]})

    request = ModelRequest(messages=({"role": "user", "content": "Hello"},), max_output_tokens=100, profile_digest="fixture")
    events = [event async for event in OpenAICompatibleProvider(profile, credential, httpx.MockTransport(handle)).generate(request)]
    assert events[-1].kind == "completed"


@pytest.mark.asyncio
async def test_openai_compatible_chat_sends_text_and_decodes_tool_call():
    profile = resolve_profile(load_config(Path(__file__).resolve().parents[1] / "ion.toml"), "groq-qwen-dev", "product")

    def handle(request):
        assert request.url.path == "/openai/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer fixture-key"
        body = json.loads(request.content)
        assert body["model"] == profile.model_id
        assert body["messages"] == [{"role": "user", "content": "Read a file"}]
        assert body["tools"][0]["function"]["name"] == "file_read"
        return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [{"id": "c1", "function": {"name": "file_read", "arguments": '{"relative_path":"a.py"}'}}]}}]})

    request = ModelRequest(messages=({"role": "user", "content": "Read a file"},), tools=({"type": "function", "function": {"name": "file_read"}},), max_output_tokens=200, profile_digest="fixture")
    events = [item async for item in OpenAICompatibleProvider(profile, "fixture-key", httpx.MockTransport(handle)).generate(request)]
    assert [item.kind for item in events] == ["tool_call", "completed"]
    assert events[0].arguments == {"relative_path": "a.py"}


@pytest.mark.asyncio
async def test_provider_auth_error_hides_response_body():
    profile = resolve_profile(load_config(Path(__file__).resolve().parents[1] / "ion.toml"), "deepseek-direct", "product")
    transport = httpx.MockTransport(lambda _: httpx.Response(401, text="fixture-key rejected"))
    request = ModelRequest(messages=({"role": "user", "content": "Hello"},), max_output_tokens=100, profile_digest="fixture")
    events = [item async for item in OpenAICompatibleProvider(profile, "fixture-key", transport).generate(request)]
    assert events[0].kind == "error"
    assert "fixture-key" not in events[0].error


@pytest.mark.asyncio
async def test_rate_limit_exposes_retry_after_without_provider_body():
    profile = resolve_profile(load_config(Path(__file__).resolve().parents[1] / "ion.toml"), "groq-qwen-dev", "product")
    transport = httpx.MockTransport(lambda _: httpx.Response(429, headers={"retry-after": "7"}, text="private provider details"))
    request = ModelRequest(messages=({"role": "user", "content": "Hello"},), max_output_tokens=100, profile_digest="fixture")
    events = [item async for item in OpenAICompatibleProvider(profile, "fixture-key", transport).generate(request)]
    assert events[0].error == "provider rate limit"
    assert events[0].retry_after_seconds == 7
    assert "private provider details" not in str(events[0])


@pytest.mark.asyncio
async def test_provider_http_errors_are_useful_but_do_not_include_body():
    profile = resolve_profile(load_config(Path(__file__).resolve().parents[1] / "ion.toml"), "groq-qwen-dev", "product")
    request = ModelRequest(messages=({"role": "user", "content": "Hello"},), max_output_tokens=100, profile_digest="fixture")
    for status, expected in ((400, "provider rejected request (400)"), (503, "provider server error (503)")):
        transport = httpx.MockTransport(lambda _, code=status: httpx.Response(code, text="private provider details"))
        events = [item async for item in OpenAICompatibleProvider(profile, "fixture-key", transport).generate(request)]
        assert events[0].error == expected
        assert "private provider details" not in str(events[0])
