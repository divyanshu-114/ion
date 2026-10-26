import os
from pathlib import Path

import pytest

from ion.config import resolve_credential, resolve_profile


@pytest.mark.parametrize("evaluation", [False, True])
@pytest.mark.parametrize("custom_env_file", [False, True])
def test_launch_ignores_dotenv_in_all_modes(tmp_path, monkeypatch, evaluation, custom_env_file):
    import ion.launcher as launcher

    monkeypatch.setattr(os, "environ", os.environ.copy())
    config_path = tmp_path / "ion.toml"
    config_path.write_text((Path(__file__).resolve().parents[1] / "ion.toml").read_text())
    env_file = tmp_path / ("credentials.env" if custom_env_file else ".env")
    env_file.write_text("AI_PROVIDER=qwen\nAI_API_KEY=local-key\nDEEPSEEK_API_KEY=local-provider-key\n")
    for name in ("AI_PROVIDER", "AI_BASE_URL", "AI_MODEL", "AI_EVALUATION", "AI_API_KEY", "AI_APIA_KEY", "DEEPSEEK_API_KEY", "ION_ENV_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ION_CONFIG", str(config_path))
    if custom_env_file:
        monkeypatch.setenv("ION_ENV_FILE", str(env_file))
    if evaluation:
        monkeypatch.setenv("AI_API_KEY", "judge-key")
        monkeypatch.setenv("AI_PROVIDER", "qwen")
    monkeypatch.setattr(launcher.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(launcher.sys.stdout, "isatty", lambda: True)

    class CaptureApp:
        def __init__(self, config, workspace_root):
            mode = "evaluation" if config.evaluation_profile else "product"
            assert mode == ("evaluation" if evaluation else "product")
            profile = resolve_profile(config, config.evaluation_profile or config.default_profile, mode)
            assert profile.provider == "deepseek"
            assert profile.locked == evaluation
            assert resolve_credential(profile, mode)[0] == ("judge-key" if evaluation else "")
            assert "DEEPSEEK_API_KEY" not in os.environ
            assert os.environ.get("AI_API_KEY") == ("judge-key" if evaluation else None)

        def run(self):
            pass

    monkeypatch.setattr(launcher, "IonApp", CaptureApp)
    assert launcher.main() == 0


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


def test_misspelled_api_key_reports_correct_name_without_disclosing_value(tmp_path, monkeypatch, capsys):
    import ion.launcher as launcher

    monkeypatch.setattr(os, 'environ', os.environ.copy())
    for name in ('AI_API_KEY', 'AI_APIA_KEY', 'AI_PROVIDER', 'AI_MODEL', 'AI_BASE_URL', 'AI_EVALUATION'):
        monkeypatch.delenv(name, raising=False)
    config_path = tmp_path / 'ion.toml'
    config_path.write_text((Path(__file__).resolve().parents[1] / 'ion.toml').read_text())
    env_file = tmp_path / '.env'
    env_file.write_text('')
    monkeypatch.setenv('ION_CONFIG', str(config_path))
    monkeypatch.setenv('ION_ENV_FILE', str(env_file))
    monkeypatch.setenv('AI_APIA_KEY', 'fixture-typo-secret')
    monkeypatch.setattr(launcher.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(launcher.sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr(launcher, 'IonApp', lambda **kwargs: pytest.fail('Invalid credential name must be explained before TUI launch'))
    assert launcher.main() == 2
    error = capsys.readouterr().err
    assert 'AI_APIA_KEY' in error and 'AI_API_KEY' in error
    assert 'fixture-typo-secret' not in error
