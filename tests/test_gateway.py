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
async def test_universal_key_reaches_only_configured_evaluation_endpoint(tmp_path, monkeypatch, provider, endpoint):
    config_path = tmp_path / "ion.toml"
    config_path.write_text(f'''schema_version = 1
default_profile = "committee"
evaluation_profile = "committee"
[profiles.committee]
provider = "{provider}"
base_url = "{endpoint}"
model = "judge-model"
locked = true
context_window = 8192
max_output_tokens = 1024
''')
    monkeypatch.setenv("AI_PROVIDER", "unrelated")
    monkeypatch.setenv("AI_BASE_URL", "https://unrelated.example/v1")
    monkeypatch.setenv("AI_MODEL", "substitute-model")
    monkeypatch.setenv("AI_API_KEY", "judge-key")
    monkeypatch.setenv("AI_EVALUATION", "1")
    config = load_config(config_path)
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


@pytest.mark.asyncio
async def test_key_only_submission_routes_direct_deepseek_credential(monkeypatch):
    from ion.config import apply_environment

    monkeypatch.setenv('AI_API_KEY', 'fixture-deepseek-key')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'stale-openrouter-key')
    config = load_config(Path(__file__).resolve().parents[1] / 'ion.toml', use_environment=False)
    config = apply_environment(config, force_evaluation=True)
    profile = resolve_profile(config, config.evaluation_profile, 'evaluation')
    credential, _ = resolve_credential(profile, 'evaluation')
    requests = []

    def handle(request):
        requests.append(request)
        assert str(request.url) == 'https://api.deepseek.com/chat/completions'
        assert request.headers['Authorization'] == 'Bearer fixture-deepseek-key'
        body = json.loads(request.content)
        assert body['model'] == 'deepseek-flash'
        assert body['thinking'] == {'type': 'disabled'}
        return httpx.Response(200, json={'choices': [{'message': {'content': 'Ready'}}]})

    request = ModelRequest(messages=({'role': 'user', 'content': 'Hello'},), max_output_tokens=100, profile_digest='fixture')
    events = [event async for event in OpenAICompatibleProvider(profile, credential, httpx.MockTransport(handle)).generate(request)]
    assert len(requests) == 1
    assert events[-1].kind == 'completed'
    assert profile.locked


@pytest.mark.asyncio
async def test_direct_deepseek_tool_result_can_continue_without_reasoning_history(tmp_path):
    from ion.artifacts import ArtifactStore
    from ion.contracts import TaskSpec
    from ion.engine import Engine
    from ion.processes import CommandSupervisor
    from ion.tools.registry import ToolDispatcher
    from ion.workspace import Workspace

    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'note.txt').write_text('The service handles scheduled jobs.\n')
    config = load_config(Path(__file__).resolve().parents[1] / 'ion.toml', use_environment=False)
    profile = resolve_profile(config, 'deepseek-direct', 'product')
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body['thinking'] == {'type': 'disabled'}
        if len(requests) == 1:
            tool, arguments = 'file_read', {'relative_path': 'note.txt'}
        else:
            assert any(message['role'] == 'tool' and 'scheduled jobs' in message['content'] for message in body['messages'])
            tool, arguments = 'finish_request', {'summary': 'The service handles scheduled jobs.'}
        return httpx.Response(200, json={'choices': [{'finish_reason': 'tool_calls', 'message': {
            'content': None, 'tool_calls': [{'id': f'call-{len(requests)}', 'type': 'function',
                                          'function': {'name': tool, 'arguments': json.dumps(arguments)}}],
        }}]})

    workspace = Workspace.capture(repo)
    artifacts = ArtifactStore(tmp_path / 'artifacts')
    dispatcher = ToolDispatcher(workspace, artifacts, CommandSupervisor(workspace, artifacts))
    provider = OpenAICompatibleProvider(profile, 'fixture-key', httpx.MockTransport(handle))
    result = await Engine(config, provider, dispatcher).run(
        TaskSpec(text='Explain the service', repo_path=str(repo), profile_name='deepseek-direct'))
    assert len(requests) == 2
    assert result.outcome == 'verified'
    assert 'scheduled jobs' in result.summary
