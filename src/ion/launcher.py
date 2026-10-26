from __future__ import annotations

import os
import sys
from pathlib import Path

from ion.config import apply_environment, load_config
from ion.tui.app import IonApp


def main() -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("Ion needs an interactive terminal. Run make run in a terminal window.", file=sys.stderr)
        return 2
    workspace_root = Path.cwd().resolve(strict=True)
    config_path = Path(os.environ.get("ION_CONFIG", Path(__file__).resolve().parents[2] / "ion.toml"))
    config = load_config(config_path, use_environment=False)
    # Credentials come from the process environment or the TUI, never dotenv.
    supplied_key = bool(os.environ.get("AI_API_KEY", "").strip())
    if not supplied_key and os.environ.get("AI_APIA_KEY", "").strip():
        print("AI_APIA_KEY is not supported. Export AI_API_KEY (without the extra A), then run make run.", file=sys.stderr)
        return 2
    config = apply_environment(config, force_evaluation=supplied_key)
    IonApp(config=config, workspace_root=workspace_root).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
