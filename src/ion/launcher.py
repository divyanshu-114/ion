from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from ion.config import apply_environment, evaluation_requested, load_config
from ion.tui.app import IonApp


def load_local_env(config_path: Path, evaluation: bool) -> None:
    if evaluation:
        return
    env_path = Path(os.environ.get("ION_ENV_FILE", config_path.parent / ".env"))
    load_dotenv(dotenv_path=env_path, override=False)


def main() -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("Ion needs an interactive terminal. Run make run in a terminal window.", file=sys.stderr)
        return 2
    workspace_root = Path.cwd().resolve(strict=True)
    config_path = Path(os.environ.get("ION_CONFIG", Path(__file__).resolve().parents[2] / "ion.toml"))
    config = load_config(config_path, use_environment=False)
    # The official launch contract supplies only AI_API_KEY. Capture its
    # presence before dotenv loading so local credentials cannot affect it.
    supplied_key = bool(os.environ.get("AI_API_KEY", "").strip())
    load_local_env(config_path, evaluation=supplied_key or bool(config.evaluation_profile) or evaluation_requested())
    config = apply_environment(config, force_evaluation=supplied_key)
    IonApp(config=config, workspace_root=workspace_root).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
