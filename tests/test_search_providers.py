"""
tests/test_search_providers.py — Unit tests for multi-provider search with fallback chain.
"""

import asyncio
import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _reload_module():
    """Force-reload search_providers so env var patches take effect."""
    if "curator.search_providers" in sys.modules:
        del sys.modules["curator.search_providers"]
    import curator.search_providers as m

    return m


SCOPE = {"keywords": ["test"]}


# ─── Grok provider ────────────────────────────────────────────────────────────


class TestSearchGrok:
    def test_returns_result_on_success(self):
        import curator.search_providers as m

        with patch.object(m, "_chat", return_value="grok answer") as mock_chat:
            result = m._search_grok("what is openviking?", SCOPE)

        assert result == "grok answer"
        mock_chat.assert_called_once()

    def test_prompt_contains_query(self):
        import curator.search_providers as m

        captured = {}

        def fake_chat(base, key, model, messages, timeout=90):
            captured["messages"] = messages
            return "ok"

        with patch.object(m, "_chat", side_effect=fake_chat):
            m._search_grok("unique-query-xyz", SCOPE)

        user_content = captured["messages"][-1]["content"]
        assert "unique-query-xyz" in user_content

    def test_propagates_exception(self):
        import curator.search_providers as m

        with patch.object(m, "_chat", side_effect=RuntimeError("timeout")):
            with pytest.raises(RuntimeError, match="timeout"):
                m._search_grok("query", SCOPE)


# ─── DuckDuckGo provider ──────────────────────────────────────────────────────


class TestSearchDuckDuckGo:
    def _make_ddg_results(self):
        return [
            {"title": "Result One", "href": "https://example.com/1", "body": "Body one"},
            {"title": "Result Two", "href": "https://example.com/2", "body": "Body two"},
        ]

    def test_returns_formatted_text(self):
        import curator.search_providers as m

        mock_ddgs = MagicMock()
        mock_ddgs.return_value.text.return_value = self._make_ddg_results()

        with patch.dict(sys.modules, {"duckduckgo_search": MagicMock(DDGS=mock_ddgs)}):
            # Need to reimport since DDGS is imported at call time
            result = m._search_duckduckgo("python packaging", SCOPE)

        assert "Result One" in result
        assert "https://example.com/1" in result
        assert "Body one" in result
        assert "Result Two" in result

    def test_empty_results_returns_empty_string(self):
        import curator.search_providers as m

        mock_ddgs = MagicMock()
        mock_ddgs.return_value.text.return_value = []

        with patch.dict(sys.modules, {"duckduckgo_search": MagicMock(DDGS=mock_ddgs)}):
            result = m._search_duckduckgo("unknown topic", SCOPE)

        assert result == ""

    def test_import_error_raises_import_error(self):
        """If duckduckgo_search package is missing, _search_duckduckgo raises ImportError."""
        import curator.search_providers as m

        # Simulate missing package by temporarily removing it from sys.modules
        orig = sys.modules.pop("duckduckgo_search", None)
        try:
            # Patch builtins.__import__ to raise ImportError for duckduckgo_search
            real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

            def fake_import(name, *args, **kwargs):
                if name == "duckduckgo_search":
                    raise ImportError("No module named 'duckduckgo_search'")
                return real_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=fake_import):
                with pytest.raises(ImportError, match="duckduckgo-search"):
                    m._search_duckduckgo("query", SCOPE)
        finally:
            if orig is not None:
                sys.modules["duckduckgo_search"] = orig


# ─── Tavily provider ──────────────────────────────────────────────────────────


class TestSearchTavily:
    def _make_tavily_response(self):
        return {
            "results": [
                {"title": "Tavily Result", "url": "https://tavily.com/r1", "content": "Some content"},
            ]
        }

    def test_returns_formatted_text(self, monkeypatch):
        import curator.search_providers as m

        monkeypatch.setenv("CURATOR_TAVILY_KEY", "tvly-test-key")

        mock_client_cls = MagicMock()
        mock_client_cls.return_value.search.return_value = self._make_tavily_response()

        with patch.dict(sys.modules, {"tavily": MagicMock(TavilyClient=mock_client_cls)}):
            result = m._search_tavily("AI news", SCOPE)

        assert "Tavily Result" in result
        assert "https://tavily.com/r1" in result
        assert "Some content" in result

    def test_missing_key_raises_runtime_error(self, monkeypatch):
        import curator.search_providers as m

        monkeypatch.delenv("CURATOR_TAVILY_KEY", raising=False)

        mock_tavily = MagicMock()
        with patch.dict(sys.modules, {"tavily": mock_tavily}):
            with pytest.raises(RuntimeError, match="CURATOR_TAVILY_KEY"):
                m._search_tavily("query", SCOPE)

    def test_empty_results_returns_empty_string(self, monkeypatch):
        import curator.search_providers as m

        monkeypatch.setenv("CURATOR_TAVILY_KEY", "tvly-test-key")

        mock_client_cls = MagicMock()
        mock_client_cls.return_value.search.return_value = {"results": []}

        with patch.dict(sys.modules, {"tavily": MagicMock(TavilyClient=mock_client_cls)}):
            result = m._search_tavily("nothing", SCOPE)

        assert result == ""

    def test_import_error_raises_import_error(self, monkeypatch):
        import curator.search_providers as m

        monkeypatch.setenv("CURATOR_TAVILY_KEY", "tvly-test-key")

        real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

        def fake_import(name, *args, **kwargs):
            if name == "tavily":
                raise ImportError("No module named 'tavily'")
            return real_import(name, *args, **kwargs)

        # Remove cached module if present
        orig = sys.modules.pop("tavily", None)
        try:
            with patch("builtins.__import__", side_effect=fake_import):
                with pytest.raises(ImportError, match="tavily-python"):
                    m._search_tavily("query", SCOPE)
        finally:
            if orig is not None:
                sys.modules["tavily"] = orig


# ─── Fallback chain ────────────────────────────────────────────────────────────


class TestFallbackChain:
    def test_first_provider_success_returns_immediately(self, monkeypatch):
        monkeypatch.setenv("CURATOR_SEARCH_PROVIDERS", "grok,duckduckgo")
        import curator.search_providers as m

        with (
            patch.object(m, "_search_grok", return_value="grok result") as mock_grok,
            patch.object(m, "_search_duckduckgo", return_value="ddg result") as mock_ddg,
        ):
            result = m.search("query", SCOPE)

        assert result == "grok result"
        mock_grok.assert_called_once()
        mock_ddg.assert_not_called()

    def test_first_provider_fails_fallback_to_second(self, monkeypatch):
        monkeypatch.setenv("CURATOR_SEARCH_PROVIDERS", "grok,duckduckgo")
        import curator.search_providers as m

        with (
            patch.object(m, "_search_grok", side_effect=RuntimeError("grok down")),
            patch.object(m, "_search_duckduckgo", return_value="ddg result"),
        ):
            result = m.search("query", SCOPE)

        assert result == "ddg result"

    def test_first_provider_empty_fallback_to_second(self, monkeypatch):
        """Empty string result (not exception) should also trigger fallback."""
        monkeypatch.setenv("CURATOR_SEARCH_PROVIDERS", "grok,duckduckgo")
        import curator.search_providers as m

        with (
            patch.object(m, "_search_grok", return_value="   "),
            patch.object(m, "_search_duckduckgo", return_value="ddg result"),
        ):
            result = m.search("query", SCOPE)

        assert result == "ddg result"

    def test_all_providers_fail_returns_empty_string(self, monkeypatch):
        monkeypatch.setenv("CURATOR_SEARCH_PROVIDERS", "grok,duckduckgo")
        import curator.search_providers as m

        with (
            patch.object(m, "_search_grok", side_effect=RuntimeError("grok down")),
            patch.object(m, "_search_duckduckgo", side_effect=RuntimeError("ddg down")),
        ):
            result = m.search("query", SCOPE)

        assert result == ""

    def test_import_error_skips_provider_silently(self, monkeypatch):
        """Provider with missing package should be skipped, not crash."""
        monkeypatch.setenv("CURATOR_SEARCH_PROVIDERS", "duckduckgo,grok")
        import curator.search_providers as m

        with (
            patch.object(m, "_search_duckduckgo", side_effect=ImportError("duckduckgo-search not installed")),
            patch.object(m, "_search_grok", return_value="grok fallback"),
        ):
            result = m.search("query", SCOPE)

        assert result == "grok fallback"

    def test_unknown_provider_skipped_with_warning(self, monkeypatch, caplog):
        monkeypatch.setenv("CURATOR_SEARCH_PROVIDERS", "nonexistent,grok")
        import curator.search_providers as m

        with patch.object(m, "_search_grok", return_value="grok result"):
            import logging

            with caplog.at_level(logging.WARNING, logger="curator"):
                result = m.search("query", SCOPE)

        assert result == "grok result"
        assert any("nonexistent" in r.message for r in caplog.records)

    def test_explicit_provider_arg_bypasses_chain(self, monkeypatch):
        """When provider= is specified explicitly, only that one is used."""
        monkeypatch.setenv("CURATOR_SEARCH_PROVIDERS", "grok,duckduckgo")
        import curator.search_providers as m

        with (
            patch.object(m, "_search_grok", return_value="grok result") as mock_grok,
            patch.object(m, "_search_duckduckgo", return_value="ddg result") as mock_ddg,
        ):
            result = m.search("query", SCOPE, provider="duckduckgo")

        assert result == "ddg result"
        mock_grok.assert_not_called()
        mock_ddg.assert_called_once()

    def test_three_provider_chain_second_wins(self, monkeypatch):
        monkeypatch.setenv("CURATOR_SEARCH_PROVIDERS", "grok,duckduckgo,tavily")
        import curator.search_providers as m

        with (
            patch.object(m, "_search_grok", side_effect=RuntimeError("grok down")),
            patch.object(m, "_search_duckduckgo", return_value="ddg result"),
            patch.object(m, "_search_tavily", return_value="tavily result") as mock_tav,
        ):
            result = m.search("query", SCOPE)

        assert result == "ddg result"
        mock_tav.assert_not_called()


# ─── get_provider (backward compat) ───────────────────────────────────────────


class TestGetProvider:
    def test_get_known_provider(self):
        import curator.search_providers as m

        fn = m.get_provider("grok")
        assert callable(fn)

    def test_get_unknown_provider_raises(self):
        import curator.search_providers as m

        with pytest.raises(ValueError, match="Unknown search provider"):
            m.get_provider("bogus_provider")

    def test_all_known_providers_registered(self):
        import curator.search_providers as m

        for name in ("grok", "oai", "duckduckgo", "tavily"):
            assert name in m._PROVIDERS, f"{name} not in _PROVIDERS"


# ─── Concurrent search ────────────────────────────────────────────────────────


class TestSearchConcurrent:
    def test_returns_first_non_empty_result(self, monkeypatch):
        import curator.search_providers as m

        async def fake_fast_non_empty(name, query, scope):
            await asyncio.sleep(0.01)
            return name, "winner"

        async def fake_slow_empty(name, query, scope):
            await asyncio.sleep(0.05)
            return name, ""

        async def fake_dispatch(name, query, scope):
            if name == "grok":
                return await fake_slow_empty(name, query, scope)
            return await fake_fast_non_empty(name, query, scope)

        monkeypatch.setattr(m, "_async_search_provider", fake_dispatch)
        result = m.search_concurrent("q", SCOPE, providers=["grok", "duckduckgo"], timeout=0.2)
        assert result == "winner"

    def test_all_timeout_returns_empty_string(self, monkeypatch):
        import curator.search_providers as m

        async def fake_very_slow(name, query, scope):
            await asyncio.sleep(0.2)
            return name, "late"

        monkeypatch.setattr(m, "_async_search_provider", fake_very_slow)
        result = m.search_concurrent("q", SCOPE, providers=["grok", "duckduckgo"], timeout=0.05)
        assert result == ""

    def test_single_provider_success(self, monkeypatch):
        import curator.search_providers as m

        async def fake_single(name, query, scope):
            return name, "single-ok"

        monkeypatch.setattr(m, "_async_search_provider", fake_single)
        result = m.search_concurrent("q", SCOPE, providers=["grok"], timeout=0.2)
        assert result == "single-ok"

    def test_provider_exception_degrades_and_other_provider_wins(self, monkeypatch):
        import curator.search_providers as m

        async def fake_dispatch(name, query, scope):
            if name == "grok":
                raise RuntimeError("boom")
            await asyncio.sleep(0.01)
            return name, "fallback-ok"

        monkeypatch.setattr(m, "_async_search_provider", fake_dispatch)
        result = m.search_concurrent("q", SCOPE, providers=["grok", "duckduckgo"], timeout=0.2)
        assert result == "fallback-ok"
