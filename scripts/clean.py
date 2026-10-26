from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parent.parent
for name in ("build", "dist", ".pytest_cache", ".mypy_cache", ".ruff_cache", "testion"):
    path = ROOT / name
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
