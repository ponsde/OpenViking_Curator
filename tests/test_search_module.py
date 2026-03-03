from __future__ import annotations


def test_external_search_sequential(monkeypatch):
    from curator import search as mod

    monkeypatch.setattr("curator.config.SEARCH_CONCURRENT", False)
    monkeypatch.setattr("curator.search_providers.search", lambda q, s: "seq_result")
    monkeypatch.setattr("curator.search_providers.search_concurrent", lambda q, s: "con_result")

    out = mod.external_search("q", {"domain": "tech"})
    assert out == "seq_result"


def test_external_search_concurrent(monkeypatch):
    from curator import search as mod

    monkeypatch.setattr("curator.config.SEARCH_CONCURRENT", True)
    monkeypatch.setattr("curator.search_providers.search", lambda q, s: "seq_result")
    monkeypatch.setattr("curator.search_providers.search_concurrent", lambda q, s: "con_result")

    out = mod.external_search("q", {"domain": "tech"})
    assert out == "con_result"


def test_cross_validate_parses_warnings(monkeypatch):
    from curator import search as mod

    monkeypatch.setattr("curator.search.JUDGE_MODELS", ["m1"])
    monkeypatch.setattr(
        "curator.search.chat",
        lambda *args,
        **kwargs: '{"claims":[{"claim":"A","risk":"high"},{"claim":"B","risk":"medium"},{"claim":"C","risk":"low"}],"summary":"ok"}',
    )

    out = mod.cross_validate("query", "external", {"domain": "tech"})
    assert out["validated"] == "external"
    assert "[⚠️ high] A" in out["warnings"]
    assert "[❓ medium] B" in out["warnings"]
    assert all("C" not in w for w in out["warnings"])


def test_cross_validate_handles_bad_json(monkeypatch):
    from curator import search as mod

    monkeypatch.setattr("curator.search.JUDGE_MODELS", ["m1"])
    monkeypatch.setattr("curator.search.chat", lambda *args, **kwargs: "not-json")

    out = mod.cross_validate("query", "external", {"domain": "tech"})
    assert out == {"validated": "external", "warnings": []}


def test_cross_validate_all_models_fail(monkeypatch):
    from curator import search as mod

    monkeypatch.setattr("curator.search.JUDGE_MODELS", ["m1", "m2"])

    def _boom(*args, **kwargs):
        raise RuntimeError("llm down")

    monkeypatch.setattr("curator.search.chat", _boom)

    out = mod.cross_validate("query", "external", {"domain": "tech"})
    assert out == {"validated": "external", "warnings": []}
