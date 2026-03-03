from __future__ import annotations


def test_extract_json_nested():
    from curator.review import _extract_json

    text = 'prefix {"a": {"b": 1}, "s": "x}y"} suffix'
    got = _extract_json(text)
    assert got == '{"a": {"b": 1}, "s": "x}y"}'


def test_extract_json_none_when_missing():
    from curator.review import _extract_json

    assert _extract_json("no braces") is None


def test_parse_judge_output_none():
    from curator.review import _parse_judge_output

    out = _parse_judge_output(None, fallback_reason="no_resp")
    assert out.passed is False
    assert out.reason == "no_resp"


def test_parse_judge_output_bad_json():
    from curator.review import _parse_judge_output

    out = _parse_judge_output("not a json", fallback_reason="bad")
    assert out.passed is False
    assert out.reason == "bad"


def test_parse_judge_output_valid_json():
    from curator.review import _parse_judge_output

    raw = '{"pass": true, "reason": "ok", "trust": 7, "freshness": "recent", "summary": "s", "markdown": "m", "has_conflict": false, "conflict_summary": "", "conflict_points": []}'
    out = _parse_judge_output(raw)
    d = out.to_pipeline_dict()
    assert d["pass"] is True
    assert d["trust"] == 7
    assert d["freshness"] == "recent"
