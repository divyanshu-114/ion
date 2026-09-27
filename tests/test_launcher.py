import os
from pathlib import Path

import pytest

from ion.config import resolve_credential, resolve_profile
from ion.launcher import load_local_env


@pytest.mark.parametrize("evaluation", [False, True])
def test_launch_applies_routing_after_dotenv_and_skips_it_in_evaluation(tmp_path, monkeypatch, evaluation):
    import ion.launcher as launcher

    # dotenv mutates os.environ directly; isolate values it adds as well.
    monkeypatch.setattr(os, "environ", os.environ.copy())
    config_path = tmp_path / "ion.toml"
    config_path.write_text((Path(__file__).resolve().parents[1] / "ion.toml").read_text())
    (tmp_path / ".env").write_text("AI_PROVIDER=qwen\nAI_API_KEY=local-key\n")
    for name in ("AI_PROVIDER", "AI_BASE_URL", "AI_MODEL", "AI_EVALUATION", "AI_API_KEY", "ION_ENV_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ION_CONFIG", str(config_path))
    if evaluation:
        monkeypatch.setenv("AI_EVALUATION", "1")
        monkeypatch.setenv("AI_PROVIDER", "deepseek")
        monkeypatch.setenv("AI_API_KEY", "judge-key")
    monkeypatch.setattr(launcher.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(launcher.sys.stdout, "isatty", lambda: True)

    class CaptureApp:
        def __init__(self, config, workspace_root):
            mode = "evaluation" if config.evaluation_profile else "product"
            profile = resolve_profile(config, config.evaluation_profile or config.default_profile, mode)
            assert profile.provider == ("deepseek" if evaluation else "qwen")
            assert profile.locked == evaluation
            assert resolve_credential(profile, mode) == ("judge-key" if evaluation else "local-key", "AI_API_KEY")

        def run(self):
            pass

    monkeypatch.setattr(launcher, "IonApp", CaptureApp)
    assert launcher.main() == 0


def test_local_dotenv_is_product_only_and_does_not_override_process_environment(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("GROQ_API_KEY=dotenv-value\nOPENROUTER_API_KEY=openrouter-value\n")
    monkeypatch.delenv("ION_ENV_FILE", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "shell-value")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    try:
        load_local_env(tmp_path / "ion.toml", evaluation=False)
        assert os.environ["GROQ_API_KEY"] == "shell-value"
        assert os.environ["OPENROUTER_API_KEY"] == "openrouter-value"
    finally:
        os.environ.pop("OPENROUTER_API_KEY", None)


def test_locked_evaluation_does_not_load_local_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("AI_API_KEY=local-value\n")
    monkeypatch.delenv("AI_API_KEY", raising=False)
    load_local_env(tmp_path / "ion.toml", evaluation=True)
    assert "AI_API_KEY" not in os.environ


@pytest.mark.parametrize("directory", ["first-repo", "second repo"])
def test_launcher_uses_current_repo_not_installation_or_environment(tmp_path, monkeypatch, directory):
    import ion.launcher as launcher

    workspace = tmp_path / directory
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("ION_REPO", str(tmp_path / "wrong-repo"))
    for name in ("ION_CONFIG", "AI_PROVIDER", "AI_BASE_URL", "AI_MODEL", "AI_EVALUATION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AI_API_KEY", "fixture-key")
    monkeypatch.setattr(launcher.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(launcher.sys.stdout, "isatty", lambda: True)

    class CaptureApp:
        def __init__(self, config, workspace_root):
            assert workspace_root == workspace.resolve()

        def run(self):
            pass

    monkeypatch.setattr(launcher, "IonApp", CaptureApp)
    assert launcher.main() == 0


def test_key_only_launch_locks_configured_model_and_ignores_local_credentials(tmp_path, monkeypatch):
    import ion.launcher as launcher

    monkeypatch.setattr(os, "environ", os.environ.copy())
    for name in ("AI_PROVIDER", "AI_BASE_URL", "AI_MODEL", "AI_EVALUATION", "ION_ENV_FILE"):
        monkeypatch.delenv(name, raising=False)
    config_path = tmp_path / "ion.toml"
    config_path.write_text('''schema_version = 1
default_profile = "committee"
[profiles.committee]
provider = "qwen"
base_url = "https://committee.example/v1"
model = "committee-qwen"
api_key_env = "DASHSCOPE_API_KEY"
context_window = 32768
max_output_tokens = 4096
''')
    (tmp_path / ".env").write_text("AI_PROVIDER=deepseek\nAI_API_KEY=local-key\n")
    monkeypatch.setenv("ION_CONFIG", str(config_path))
    monkeypatch.setenv("AI_API_KEY", "committee-key")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "stale-local-key")
    monkeypatch.setattr(launcher.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(launcher.sys.stdout, "isatty", lambda: True)

    class CaptureApp:
        def __init__(self, config, workspace_root):
            assert config.evaluation_profile
            profile = resolve_profile(config, config.evaluation_profile, "evaluation")
            assert profile.locked
            assert profile.model_id == "committee-qwen"
            assert profile.endpoint == "https://committee.example/v1"
            assert profile.context_window == 32768
            assert resolve_credential(profile, "evaluation") == ("committee-key", "AI_API_KEY")
            assert "AI_PROVIDER" not in os.environ

        def run(self):
            pass

    monkeypatch.setattr(launcher, "IonApp", CaptureApp)
    assert launcher.main() == 0
