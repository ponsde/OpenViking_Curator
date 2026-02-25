"""Tests for retrieval_v2.rerank_with_feedback()."""
import pytest
from unittest.mock import patch


def _make_items(*uris_scores):
    """Helper: build item list [(uri, score), ...]"""
    return [{"uri": u, "score": s, "abstract": f"abstract of {u}"} for u, s in uris_scores]


def _patch_fb(data: dict):
    """Return a context manager that patches feedback_store.load()."""
    return patch("feedback_store.load", return_value=data)


class TestRerankWithFeedback:
    """rerank_with_feedback 核心行为测试。"""

    def test_no_feedback_returns_unchanged_order(self):
        """没有 feedback 记录时，顺序和分数不变。"""
        from curator.retrieval_v2 import rerank_with_feedback

        with _patch_fb({}):
            items = _make_items(("a", 0.9), ("b", 0.7), ("c", 0.5))
            result = rerank_with_feedback(items)

        assert [r["uri"] for r in result] == ["a", "b", "c"]
        assert result[0]["score"] == 0.9

    def test_adopt_boosts_score(self):
        """adopt 记录应该提升 score。"""
        from curator.retrieval_v2 import rerank_with_feedback

        fb = {"b": {"up": 0, "down": 0, "adopt": 5}}
        with _patch_fb(fb):
            items = _make_items(("a", 0.80), ("b", 0.75))
            result = rerank_with_feedback(items)

        # b 的 adopt boost 应该让它超过 a
        uris = [r["uri"] for r in result]
        assert uris[0] == "b", f"期望 b 排第一，实际: {uris}"
        assert result[0]["score"] > 0.75

    def test_down_penalizes_score(self):
        """down 记录应该降低 score，使其排名靠后。"""
        from curator.retrieval_v2 import rerank_with_feedback

        fb = {"a": {"up": 0, "down": 10, "adopt": 0}}
        with _patch_fb(fb):
            items = _make_items(("a", 0.80), ("b", 0.78))
            result = rerank_with_feedback(items)

        uris = [r["uri"] for r in result]
        assert uris[0] == "b", f"期望 b 因 a 被降权后排第一，实际: {uris}"

    def test_feedback_delta_recorded(self):
        """feedback 变动应记录在 _feedback_delta 字段。"""
        from curator.retrieval_v2 import rerank_with_feedback

        fb = {"x": {"up": 3, "down": 1, "adopt": 2}}
        with _patch_fb(fb):
            items = _make_items(("x", 0.6))
            result = rerank_with_feedback(items)

        assert "_feedback_delta" in result[0]
        assert result[0]["_feedback_delta"] > 0  # up+adopt 多于 down，delta 应为正

    def test_empty_list_passthrough(self):
        """空列表直接返回，不报错。"""
        from curator.retrieval_v2 import rerank_with_feedback

        with _patch_fb({}):
            assert rerank_with_feedback([]) == []

    def test_missing_score_field_handled(self):
        """缺少 score 字段的 item 不应崩溃。"""
        from curator.retrieval_v2 import rerank_with_feedback

        with _patch_fb({}):
            items = [{"uri": "no-score-item", "abstract": "test"}]
            result = rerank_with_feedback(items)
        assert len(result) == 1

    def test_score_stays_dominant(self):
        """反馈权重保守（max 0.10），高分资源不会被低分+高feedback超越。"""
        from curator.retrieval_v2 import rerank_with_feedback

        # b 有极高 adopt，但原始分远低于 a
        fb = {"b": {"up": 100, "down": 0, "adopt": 100}}
        with _patch_fb(fb):
            items = _make_items(("a", 0.95), ("b", 0.30))
            result = rerank_with_feedback(items)

        assert result[0]["uri"] == "a", "高原始分的 a 应仍排第一（OV score 主导）"

    def test_uri_without_feedback_unchanged(self):
        """没有 feedback 的 URI score 和 delta 不变。"""
        from curator.retrieval_v2 import rerank_with_feedback

        fb = {"other_uri": {"up": 5, "down": 0, "adopt": 0}}
        with _patch_fb(fb):
            items = _make_items(("no_fb_uri", 0.7))
            result = rerank_with_feedback(items)

        assert result[0]["score"] == 0.7
        assert "_feedback_delta" not in result[0]
