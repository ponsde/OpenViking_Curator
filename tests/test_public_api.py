"""Public API contract tests for curator package."""

from __future__ import annotations

import importlib

import pytest

CORE_EXPORTS = {
    "run",
    "KnowledgeBackend",
    "SearchResult",
    "SearchResponse",
    "OpenVikingBackend",
    "InMemoryBackend",
    "JudgeResult",
    "chat",
    "env",
    "log",
    "validate_config",
    "__version__",
}


def test_core_symbols_can_be_imported():
    from curator import (
        InMemoryBackend,
        JudgeResult,
        KnowledgeBackend,
        OpenVikingBackend,
        SearchResponse,
        SearchResult,
        run,
    )

    assert run is not None
    assert KnowledgeBackend is not None
    assert SearchResult is not None
    assert SearchResponse is not None
    assert OpenVikingBackend is not None
    assert InMemoryBackend is not None
    assert JudgeResult is not None


def test_deprecated_symbols_import_fail():
    with pytest.raises(ImportError):
        from curator import OVClient  # type: ignore[attr-defined]  # noqa: F401

    with pytest.raises(ImportError):
        from curator import SessionManager  # type: ignore[attr-defined]  # noqa: F401


def test_submodule_paths_are_stable_and_match_top_level_object():
    from curator import JudgeResult as top_level_judge_result
    from curator.review import JudgeResult as review_judge_result
    from curator.review import detect_conflict, judge_and_ingest

    assert top_level_judge_result is review_judge_result
    assert callable(judge_and_ingest)
    assert callable(detect_conflict)


def test_all_matches_actual_exports():
    curator = importlib.import_module("curator")

    assert set(curator.__all__) == CORE_EXPORTS
    for name in curator.__all__:
        assert getattr(curator, name) is not None
