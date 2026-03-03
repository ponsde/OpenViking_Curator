"""Shared helpers for scripts in OpenViking Curator."""

from __future__ import annotations

import re
from pathlib import Path

from curator.settings import CuratorSettings

# Shared regex used by multiple maintenance scripts
META_RE = re.compile(r"<!--\s*curator_meta:\s*(.+?)\s*-->")


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_data_dir() -> str:
    """Default data directory from settings (keeps env handling centralized)."""
    return CuratorSettings().data_path


def default_curated_dir() -> str:
    """Default curated directory from settings (keeps env handling centralized)."""
    return CuratorSettings().curated_dir
