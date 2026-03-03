"""Shared .env loader for entry points and scripts.

Centralizes dotenv parsing so there is a single implementation site.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_env(env_file: str | Path | None = None) -> Path | None:
    """Load key=value pairs into os.environ (without overwriting existing vars).

    Args:
        env_file: Optional .env file path. Defaults to project-root .env.

    Returns:
        Resolved env file path if loaded/found, else None.
    """
    target = Path(env_file) if env_file is not None else Path(__file__).resolve().parent.parent / ".env"
    if not target.exists():
        return None

    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

    return target.resolve()
