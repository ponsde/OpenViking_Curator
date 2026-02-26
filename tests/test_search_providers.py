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

        with patch.object(m, "chat", return_value="grok answer") as mock_chat:
            result = m._search_grok("what is openviking?", SCOPE)

        assert result == "grok answer"
        mock_chat.assert_called_once()

    def test_prompt_contains_query(self):
        import curator.search_providers as m

        captured = {}

        def fake_chat(base, key, model, messages, timeout=60, temperature=None):
            captured["messages"] = messages
            return "ok"

        with patch.object(m, "chat", side_effect=fake_chat):
            m._search_grok("unique-query-xyz", SCOPE)

        user_content = captured["messages"][-1]["content"]
        assert "unique-query-xyz" in user_content

    def test_propagates_exception(self):
        import curator.search_providers as m

        with patch.object(m, "chat", side_effect=RuntimeError("timeout")):
            with pytest.raises(RuntimeError, match="timeout"):
                m._search_grok("query", SCOPE)

    def test_time_context_injected_for_time_queries(self):
        """Time-sensitive queries should inject current datetime into prompt."""
        import curator.search_providers as m

        captured = {}

        def fake_chat(base, key, model, messages, timeout=60, temperature=None):
            captured["messages"] = messages
            return "ok"

        with patch.object(m, "chat", side_effect=fake_chat):
            m._search_grok("最新的 Python 3.13 特性", SCOPE)

        system_content = captured["messages"][0]["content"]
        assert "当前精确时间" in system_content

    def test_no_time_context_for_normal_queries(self):
        """Non-time-sensitive queries should not get time context."""
        import curator.search_providers as m

        captured = {}

        def fake_chat(base, key, model, messages, timeout=60, temperature=None):
            captured["messages"] = messages
            return "ok"

        with patch.object(m, "chat", side_effect=fake_chat):
            m._search_grok("how to deploy Redis", SCOPE)

        system_content = captured["messages"][0]["content"]
        assert "当前精确时间" not in system_content


# ─── DuckDuckGo provider ──────────────────────────────────────────────────────


class TestSearchDuckDuckGo:
    def _make_ddg_results(self):
        return [
            {"title": "Result One", "href": "https://example.com/1", "body": "Body one"},
            {"title": "Result Two", "href": "https://example.com/2", "body": "Body two"},
        ]

    def test_returns_structured_results(self):
        import curator.search_providers as m

        mock_ddgs = MagicMock()
        mock_ddgs.return_value.text.return_value = self._make_ddg_results()

        with patch.dict(sys.modules, {"duckduckgo_search": MagicMock(DDGS=mock_ddgs)}):
            result = m._search_duckduckgo("python packaging", SCOPE)

        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0].title == "Result One"
        assert result[0].url == "https://example.com/1"
        assert result[0].snippet == "Body one"
        assert result[1].title == "Result Two"

    def test_empty_results_returns_empty_list(self):
        import curator.search_providers as m

        mock_ddgs = MagicMock()
        mock_ddgs.return_value.text.return_value = []

        with patch.dict(sys.modules, {"duckduckgo_search": MagicMock(DDGS=mock_ddgs)}):
            result = m._search_duckduckgo("unknown topic", SCOPE)

        assert result == []

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

    def test_returns_structured_results(self):
        import curator.search_providers as m

        mock_client_cls = MagicMock()
        mock_client_cls.return_value.search.return_value = self._make_tavily_response()

        with patch.object(m, "TAVILY_KEY", "tvly-test-key"):
            with patch.dict(sys.modules, {"tavily": MagicMock(TavilyClient=mock_client_cls)}):
                result = m._search_tavily("AI news", SCOPE)

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].title == "Tavily Result"
        assert result[0].url == "https://tavily.com/r1"
        assert result[0].snippet == "Some content"

    def test_missing_key_raises_runtime_error(self):
        import curator.search_providers as m

        mock_tavily = MagicMock()
        with patch.object(m, "TAVILY_KEY", ""):
            with patch.dict(sys.modules, {"tavily": mock_tavily}):
                with pytest.raises(RuntimeError, match="CURATOR_TAVILY_KEY"):
                    m._search_tavily("query", SCOPE)

    def test_empty_results_returns_empty_list(self):
        import curator.search_providers as m

        mock_client_cls = MagicMock()
        mock_client_cls.return_value.search.return_value = {"results": []}

        with patch.object(m, "TAVILY_KEY", "tvly-test-key"):
            with patch.dict(sys.modules, {"tavily": MagicMock(TavilyClient=mock_client_cls)}):
                result = m._search_tavily("nothing", SCOPE)

        assert result == []

    def test_import_error_raises_import_error(self):
        import curator.search_providers as m

        real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

        def fake_import(name, *args, **kwargs):
            if name == "tavily":
                raise ImportError("No module named 'tavily'")
            return real_import(name, *args, **kwargs)

        orig = sys.modules.pop("tavily", None)
        try:
            with patch.object(m, "TAVILY_KEY", "tvly-test-key"):
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


# ─── SearchResult + format_results ────────────────────────────────────────────


class TestSearchResult:
    def test_format_results_basic(self):
        from curator.search_providers import SearchResult, format_results

        results = [
            SearchResult(title="Title 1", url="https://a.com", snippet="Snippet 1"),
            SearchResult(title="Title 2", url="https://b.com", snippet="Snippet 2"),
        ]
        text = format_results(results)
        assert "**Title 1**" in text
        assert "https://a.com" in text
        assert "Snippet 1" in text
        assert "**Title 2**" in text

    def test_format_results_empty(self):
        from curator.search_providers import format_results

        assert format_results([]) == ""

    def test_provider_output_to_text_string(self):
        from curator.search_providers import _provider_output_to_text

        assert _provider_output_to_text("plain text") == "plain text"

    def test_provider_output_to_text_list(self):
        from curator.search_providers import SearchResult, _provider_output_to_text

        results = [SearchResult(title="T", url="https://x.com", snippet="S")]
        text = _provider_output_to_text(results)
        assert "**T**" in text
        assert "https://x.com" in text


# ─── _parse_search_results_json ────────────────────────────────────────────


class TestParseSearchResultsJson:
    def test_parses_valid_json_array(self):
        from curator.search_providers import _parse_search_results_json

        text = '[{"title":"T1","url":"https://a.com","date":"2026-01-01","snippet":"S1"}]'
        results = _parse_search_results_json(text)
        assert results is not None
        assert len(results) == 1
        assert results[0].title == "T1"
        assert results[0].url == "https://a.com"
        assert results[0].snippet == "S1"

    def test_extracts_json_from_markdown_block(self):
        from curator.search_providers import _parse_search_results_json

        text = 'Here are the results:\n```json\n[{"title":"T","url":"https://b.com","snippet":"S"}]\n```'
        results = _parse_search_results_json(text)
        assert results is not None
        assert results[0].url == "https://b.com"

    def test_returns_none_on_no_array(self):
        from curator.search_providers import _parse_search_results_json

        assert _parse_search_results_json("just plain text, no JSON") is None
        assert _parse_search_results_json('{"key": "value"}') is None  # object not array

    def test_returns_none_on_invalid_json(self):
        from curator.search_providers import _parse_search_results_json

        assert _parse_search_results_json("[not valid json}") is None

    def test_skips_items_without_url(self):
        from curator.search_providers import _parse_search_results_json

        text = '[{"title":"T1","url":"https://a.com","snippet":"S"},{"title":"T2","snippet":"no url"}]'
        results = _parse_search_results_json(text)
        assert results is not None
        assert len(results) == 1  # second item skipped (no url)

    def test_returns_none_on_empty_results(self):
        from curator.search_providers import _parse_search_results_json

        assert _parse_search_results_json("[]") is None

    def test_grok_returns_structured_when_parseable(self, monkeypatch):
        """_search_grok returns list[WebSearchResult] when LLM outputs valid JSON."""
        import curator.search_providers as m
        from curator.search_providers import WebSearchResult

        json_response = '[{"title":"R1","url":"https://x.com","date":"","snippet":"Snippet"}]'
        monkeypatch.setattr(m, "chat", lambda *a, **kw: json_response)
        result = m._search_grok("test query", {})
        assert isinstance(result, list)
        assert isinstance(result[0], WebSearchResult)
        assert result[0].url == "https://x.com"

    def test_grok_falls_back_to_str_when_unparseable(self, monkeypatch):
        """_search_grok returns raw str when LLM outputs plain text."""
        import curator.search_providers as m

        monkeypatch.setattr(m, "chat", lambda *a, **kw: "Here are some results in plain text.")
        result = m._search_grok("test query", {})
        assert isinstance(result, str)
        assert "plain text" in result

    def test_parses_snippet_containing_closing_bracket(self):
        """Snippets with ] must not break parsing (JSONDecoder.raw_decode handles this)."""
        from curator.search_providers import _parse_search_results_json

        text = '[{"title":"T","url":"https://x.com","snippet":"Array notation: items[0]"}]'
        results = _parse_search_results_json(text)
        assert results is not None
        assert len(results) == 1
        assert "items[0]" in results[0].snippet
