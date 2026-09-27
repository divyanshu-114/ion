from pathlib import Path

import pytest

from ion.config import load_config, resolve_credential, resolve_profile


@pytest.fixture(autouse=True)
def clear_ai_settings(monkeypatch):
    for name in ("AI_PROVIDER", "AI_BASE_URL", "AI_MODEL", "AI_EVALUATION"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("provider,host,model", [
    ("deepseek", "https://api.deepseek.com", "deepseek-flash"),
    ("qwen", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    ("groq", "https://api.groq.com/openai/v1", "judge-model"),
    ("openrouter", "https://openrouter.ai/api/v1", "judge-model"),
])
def test_environment_routes_universal_key(monkeypatch, provider, host, model):
    monkeypatch.setenv("AI_PROVIDER", provider)
    monkeypatch.setenv("AI_API_KEY", "judge-key")
    if provider in {"groq", "openrouter"}:
        monkeypatch.setenv("AI_MODEL", model)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "stale-local-key")
    config = load_config(Path(__file__).resolve().parents[1] / "ion.toml")
    profile = resolve_profile(config, config.default_profile, "product")
    assert (profile.provider, profile.endpoint, profile.model_id) == (provider, host, model)
    assert resolve_credential(profile) == ("judge-key", "AI_API_KEY")


def test_custom_endpoint_and_locked_evaluation(monkeypatch):
    monkeypatch.setenv("AI_BASE_URL", "https://judge.example/v1")
    monkeypatch.setenv("AI_MODEL", "Qwen/judge-model")
    monkeypatch.setenv("AI_EVALUATION", "1")
    config = load_config(Path(__file__).resolve().parents[1] / "ion.toml")
    assert config.evaluation_profile
    profile = resolve_profile(config, config.evaluation_profile, "evaluation")
    assert profile.endpoint == "https://judge.example/v1"
    assert profile.model_id == "Qwen/judge-model"
    assert profile.locked


@pytest.mark.parametrize("settings,match", [
    ({"AI_PROVIDER": "unknown"}, "AI_BASE_URL.*AI_MODEL"),
    ({"AI_BASE_URL": "https://judge.example/v1"}, "AI_MODEL"),
    ({"AI_PROVIDER": "qwen", "AI_BASE_URL": "http://judge.example/v1"}, "HTTPS"),
    ({"AI_EVALUATION": "maybe"}, "AI_EVALUATION"),
])
def test_invalid_environment_fails_before_network(monkeypatch, settings, match):
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=match):
        load_config(Path(__file__).resolve().parents[1] / "ion.toml")


def test_profiles_use_provider_keys_and_evaluation_uses_ai_api_key(monkeypatch):
    config = load_config(Path(__file__).resolve().parents[1] / "ion.toml")
    profile = resolve_profile(config, "groq-qwen-dev", "product")
    monkeypatch.setenv("GROQ_API_KEY", "groq-only-test-value")
    monkeypatch.setenv("AI_API_KEY", "evaluation-test-value")
    assert resolve_credential(profile, "product") == ("groq-only-test-value", "GROQ_API_KEY")
    assert resolve_credential(profile, "evaluation") == ("evaluation-test-value", "AI_API_KEY")


def test_config_rejects_embedded_credential(tmp_path):
    path = tmp_path / "ion.toml"
    path.write_text('schema_version=1\ndefault_profile="x"\n[profiles.x]\nprovider="groq"\nbase_url="https://api.example/v1"\nmodel="x"\napi_key="secret"\ncontext_window=8192\nmax_output_tokens=1024\n')
    with pytest.raises(ValueError):
        load_config(path)
