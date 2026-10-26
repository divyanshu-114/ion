"""Clean build and test caches from the workspace."""

from __future__ import annotations

import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CLEAN_TARGETS = ("build", "dist", ".pytest_cache", ".mypy_cache", ".ruff_cache", "testion")


def main() -> None:
    for name in CLEAN_TARGETS:
        path = ROOT / name
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)


if __name__ == "__main__":
    main()
