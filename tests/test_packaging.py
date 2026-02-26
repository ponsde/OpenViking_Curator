"""Tests for PyPI packaging readiness."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_version_consistency():
    """All version references agree with _version.py."""
    from curator import __version__ as init_version
    from curator._version import __version__
    from curator.config import CURATOR_VERSION

    assert __version__ == init_version
    assert __version__ == CURATOR_VERSION


def test_py_typed_exists():
    """py.typed marker file exists in curator package."""
    assert (ROOT / "curator" / "py.typed").exists()


def test_prompts_included():
    """Prompt templates exist in the package."""
    prompts_dir = ROOT / "curator" / "prompts"
    assert prompts_dir.is_dir()
    assert list(prompts_dir.glob("*.prompt")), "No .prompt files found"


def test_router_config_included():
    """router_config.json exists in the package."""
    assert (ROOT / "curator" / "router_config.json").exists()


def test_curator_query_main_callable():
    """curator_query.main() is importable and callable."""
    from curator_query import main

    assert callable(main)


def test_mcp_server_main_callable():
    """mcp_server.main() is importable and callable."""
    from mcp_server import main

    assert callable(main)
