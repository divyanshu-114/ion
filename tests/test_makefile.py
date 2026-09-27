import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("target", ["setup", "run"])
@pytest.mark.parametrize("relocated", [False, True])
def test_make_uses_ion_installation_without_changing_target_workspace(tmp_path, target, relocated):
    ion_root = Path(__file__).resolve().parents[1]
    if relocated:
        checkout = tmp_path / "cloned ion"
        (checkout / "scripts").mkdir(parents=True)
        shutil.copyfile(ion_root / "Makefile", checkout / "Makefile")
        shutil.copyfile(ion_root / "scripts/bootstrap.py", checkout / "scripts/bootstrap.py")
        ion_root = checkout.resolve()
    workspace = tmp_path / "another repo"
    workspace.mkdir()
    # The target's Python project must not become uv's installation project.
    (workspace / "pyproject.toml").write_text('[project]\nname="target-project"\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "invocation.json"
    uv = bin_dir / "uv"
    uv.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
Path(os.environ["ION_TEST_CAPTURE"]).write_text(json.dumps({
    "cwd": os.getcwd(), "args": sys.argv[1:],
    "cache": os.environ["UV_CACHE_DIR"],
}))
''')
    uv.chmod(0o755)
    env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
           "ION_TEST_CAPTURE": str(capture)}
    for name in ("UV", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "MAKEFLAGS", "MFLAGS"):
        env.pop(name, None)
    result = subprocess.run(["make", "-f", str(ion_root / "Makefile"), target],
                            cwd=workspace, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    invocation = json.loads(capture.read_text())
    assert Path(invocation["cwd"]) == workspace.resolve()
    args = invocation["args"]
    assert "--project" in args
    assert Path(args[args.index("--project") + 1]) == ion_root
    assert args[0] == ("sync" if target == "setup" else "run")
    assert Path(invocation["cache"]) == ion_root / ".uv-cache"
    assert not (workspace / ".venv").exists()
