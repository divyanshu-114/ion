"""Install a project-local uv only when the host has none."""

from __future__ import annotations

import shutil
import subprocess
import sys
import venv
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LOCAL_UV = ROOT / ".uv-tools" / "bootstrap" / "bin" / "uv"


def main() -> None:
    if shutil.which("uv") or LOCAL_UV.is_file():
        return
    venv.create(LOCAL_UV.parent.parent, with_pip=True)
    subprocess.run([str(LOCAL_UV.parent / "python"), "-m", "pip", "install", "--disable-pip-version-check", "uv==0.11.8"], check=True)
    if not LOCAL_UV.is_file():
        raise RuntimeError("uv bootstrap did not create its local executable")


if __name__ == "__main__":
    main()
