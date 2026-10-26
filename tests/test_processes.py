import pytest

from ion.artifacts import ArtifactStore
from ion.processes import CommandSupervisor
from ion.workspace import Workspace


@pytest.mark.asyncio
async def test_command_supervisor_rejects_destructive_shell_and_keeps_api_keys_out(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret-value")
    supervisor = CommandSupervisor(Workspace.capture(tmp_path), ArtifactStore(tmp_path / "artifacts"), "secret-value")
    with pytest.raises(ValueError, match="execution policy"):
        await supervisor.run("rm -rf .")
    with pytest.raises(ValueError, match="execution policy"):
        await supervisor.run("python -m pytest -q && echo done")
    with pytest.raises(ValueError, match="execution policy"):
        await supervisor.run("echo safe; echo bypass")
    with pytest.raises(ValueError, match="execution policy"):
        await supervisor.run("echo $(printf leaked)")
    with pytest.raises(ValueError, match="execution policy"):
        await supervisor.run("echo `printf leaked`")
    await supervisor.close()
