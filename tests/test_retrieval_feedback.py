"""Tests for retrieval_v2.rerank_with_feedback()."""

from unittest.mock import patch

import pytest

# feedback_store has been moved into curator package
feedback_store = pytest.importorskip("curator.feedback_store", reason="curator.feedback_store not found")


def _make_items(*uris_scores):
    """Helper: build item list [(uri, score), ...]"""
    return [{"uri": u, "score": s, "abstract": f"abstract of {u}"} for u, s in uris_scores]


def _patch_fb(data: dict):
    """Return a context manager that patches feedback_store.load()."""
    return patch("curator.feedback_store.load", return_value=data)


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

    def test_mixed_up_and_down_signals(self):
        """up 和 down 混合时，delta 方向由多数决定。"""
        from curator.retrieval_v2 import rerank_with_feedback

        # down 比 up 多 → 应该是负 delta
        fb_net_negative = {"a": {"up": 1, "down": 5, "adopt": 0}}
        with _patch_fb(fb_net_negative):
            result = rerank_with_feedback(_make_items(("a", 0.8)))
        assert result[0]["_feedback_delta"] < 0, "down 占多数时 delta 应为负"

        # up 比 down 多 → 应该是正 delta
        fb_net_positive = {"b": {"up": 5, "down": 1, "adopt": 0}}
        with _patch_fb(fb_net_positive):
            result = rerank_with_feedback(_make_items(("b", 0.8)))
        assert result[0]["_feedback_delta"] > 0, "up 占多数时 delta 应为正"

    def test_score_none_handled(self):
        """score=None 的 item 不应崩溃；无 feedback 记录时原样返回。"""
        from curator.retrieval_v2 import rerank_with_feedback

        with _patch_fb({}):
            items = [{"uri": "null-score", "score": None, "abstract": "test"}]
            result = rerank_with_feedback(items)  # 不应抛出异常
        assert len(result) == 1

    def test_delta_bounded_by_feedback_weight(self):
        """delta 绝对值不超过 FEEDBACK_WEIGHT（默认 0.10）。"""
        from curator.config import FEEDBACK_WEIGHT
        from curator.retrieval_v2 import rerank_with_feedback

        # 极端正向：大量 adopt
        fb = {"a": {"up": 0, "down": 0, "adopt": 9999}}
        with _patch_fb(fb):
            result = rerank_with_feedback(_make_items(("a", 0.5)))
        assert (
            result[0]["_feedback_delta"] <= FEEDBACK_WEIGHT + 1e-6
        ), f"正向 delta 超出 FEEDBACK_WEIGHT: {result[0]['_feedback_delta']}"

        # 极端负向：大量 down
        fb2 = {"b": {"up": 0, "down": 9999, "adopt": 0}}
        with _patch_fb(fb2):
            result = rerank_with_feedback(_make_items(("b", 0.5)))
        assert (
            result[0]["_feedback_delta"] >= -FEEDBACK_WEIGHT - 1e-6
        ), f"负向 delta 超出 FEEDBACK_WEIGHT: {result[0]['_feedback_delta']}"

    def test_score_none_with_feedback(self):
        """score=None 且有 feedback 记录时，不应崩溃，delta 应被正确应用。"""
        from curator.retrieval_v2 import rerank_with_feedback

        fb = {"null-score": {"up": 1, "down": 0, "adopt": 0}}
        with _patch_fb(fb):
            items = [{"uri": "null-score", "score": None}]
            result = rerank_with_feedback(items)
        assert result[0]["score"] is not None  # None → 0.0 + delta
        assert result[0]["_feedback_delta"] > 0


class TestRerankWithFeedbackDecay:
    """Tests for stats_v2 time-decayed reranking path."""

    def _make_stats_v2(self, up_w=0.0, down_w=0.0, adopt_w=0.0, seen_w=1.0, last_decay_at=None):
        from datetime import datetime, timezone

        return {
            "up_w": up_w,
            "down_w": down_w,
            "adopt_w": adopt_w,
            "seen_w": max(seen_w, 1.0),
            "last_decay_at": last_decay_at or datetime.now(timezone.utc).isoformat(),
            "last_event_at": datetime.now(timezone.utc).isoformat(),
            "schema_version": 2,
        }

    def test_stats_v2_adopt_boosts_score(self):
        """stats_v2 路径下 adopt_w 应产生正 delta。"""
        from curator.retrieval_v2 import rerank_with_feedback

        stats = self._make_stats_v2(adopt_w=5.0, seen_w=6.0)
        fb = {"b": {"up": 0, "down": 0, "adopt": 5, "stats_v2": stats}}
        with _patch_fb(fb):
            with patch("curator.retrieval_v2.FEEDBACK_DECAY_ENABLED", True):
                items = _make_items(("a", 0.80), ("b", 0.75))
                result = rerank_with_feedback(items)

        uris = [r["uri"] for r in result]
        assert uris[0] == "b", f"期望 b 因 adopt_w 排第一, 实际: {uris}"
        assert result[0]["_feedback_delta"] > 0

    def test_stats_v2_down_penalizes_score(self):
        """stats_v2 路径下 down_w 应产生负 delta。"""
        from curator.retrieval_v2 import rerank_with_feedback

        stats = self._make_stats_v2(down_w=8.0, seen_w=9.0)
        fb = {"a": {"up": 0, "down": 8, "adopt": 0, "stats_v2": stats}}
        with _patch_fb(fb):
            with patch("curator.retrieval_v2.FEEDBACK_DECAY_ENABLED", True):
                items = _make_items(("a", 0.80), ("b", 0.78))
                result = rerank_with_feedback(items)

        uris = [r["uri"] for r in result]
        assert uris[0] == "b", f"期望 b 因 a 被降权后排第一, 实际: {uris}"

    def test_explore_bonus_for_new_content(self):
        """低曝光内容 (seen_w 小) 应获得探索加成，delta > 纯计分结果。"""
        from curator.retrieval_v2 import rerank_with_feedback

        # seen_w=1: 新内容，explore_bonus 最大
        stats_new = self._make_stats_v2(up_w=1.0, seen_w=1.0)
        # seen_w=100: 老内容，explore_bonus ≈ 0
        stats_old = self._make_stats_v2(up_w=1.0, seen_w=100.0)

        with patch("curator.retrieval_v2.FEEDBACK_DECAY_ENABLED", True):
            with patch("curator.retrieval_v2.FEEDBACK_EXPLORE_BONUS", 0.05):
                fb_new = {"x": {"up": 1, "down": 0, "adopt": 0, "stats_v2": stats_new}}
                with _patch_fb(fb_new):
                    result_new = rerank_with_feedback(_make_items(("x", 0.5)))

                fb_old = {"x": {"up": 1, "down": 0, "adopt": 0, "stats_v2": stats_old}}
                with _patch_fb(fb_old):
                    result_old = rerank_with_feedback(_make_items(("x", 0.5)))

        delta_new = result_new[0]["_feedback_delta"]
        delta_old = result_old[0]["_feedback_delta"]
        assert delta_new > delta_old, f"新内容 delta ({delta_new}) 应 > 旧内容 delta ({delta_old})"

    def test_delta_bounded_with_stats_v2(self):
        """使用 stats_v2 时 delta 仍不超出 FEEDBACK_WEIGHT。"""
        from curator.config import FEEDBACK_WEIGHT
        from curator.retrieval_v2 import rerank_with_feedback

        # Extreme positive: huge adopt, low seen
        stats = self._make_stats_v2(adopt_w=9999.0, seen_w=1.0)
        fb = {"a": {"up": 0, "down": 0, "adopt": 9999, "stats_v2": stats}}
        with _patch_fb(fb):
            with patch("curator.retrieval_v2.FEEDBACK_DECAY_ENABLED", True):
                result = rerank_with_feedback(_make_items(("a", 0.5)))

        assert result[0]["_feedback_delta"] <= FEEDBACK_WEIGHT + 1e-6

    def test_legacy_fallback_without_stats_v2(self):
        """无 stats_v2 字段时即使 decay enabled 也走旧路径，结果一致。"""
        from curator.retrieval_v2 import rerank_with_feedback

        fb = {"x": {"up": 3, "down": 1, "adopt": 2}}  # no stats_v2
        with _patch_fb(fb):
            with patch("curator.retrieval_v2.FEEDBACK_DECAY_ENABLED", True):
                result_decay = rerank_with_feedback(_make_items(("x", 0.6)))
            with patch("curator.retrieval_v2.FEEDBACK_DECAY_ENABLED", False):
                result_legacy = rerank_with_feedback(_make_items(("x", 0.6)))

        # Both should take the legacy path → identical delta
        assert result_decay[0]["_feedback_delta"] == result_legacy[0]["_feedback_delta"]


class TestFeedbackStoreDecay:
    """Tests for stats_v2 write path in feedback_store.apply()."""

    def test_apply_writes_stats_v2_when_decay_enabled(self, tmp_path, monkeypatch):
        """apply() should populate stats_v2 when decay is enabled."""
        store = tmp_path / "fb.json"
        monkeypatch.setenv("CURATOR_FEEDBACK_FILE", str(store))

        from curator import feedback_store

        with patch("curator.config.FEEDBACK_DECAY_ENABLED", True):
            feedback_store.apply("viking://test", "adopt")

        data = feedback_store.load(store)
        rec = data.get("viking://test", {})
        assert "stats_v2" in rec, "stats_v2 should be created when decay is enabled"
        assert rec["stats_v2"]["adopt_w"] == 1.0
        assert rec["stats_v2"]["seen_w"] == 1.0  # migration seeds seen_w from legacy sum

    def test_apply_no_stats_v2_when_decay_disabled(self, tmp_path, monkeypatch):
        """apply() should NOT write stats_v2 when decay is disabled."""
        store = tmp_path / "fb.json"
        monkeypatch.setenv("CURATOR_FEEDBACK_FILE", str(store))

        from curator import feedback_store

        with patch("curator.config.FEEDBACK_DECAY_ENABLED", False):
            feedback_store.apply("viking://test", "up")

        data = feedback_store.load(store)
        rec = data.get("viking://test", {})
        assert "stats_v2" not in rec, "stats_v2 should not be created when decay is disabled"
        assert rec["up"] == 1

    def test_apply_increments_existing_stats_v2(self, tmp_path, monkeypatch):
        """Second apply() (stats_v2_existed=True path) should decay then increment weights."""
        store = tmp_path / "fb.json"
        monkeypatch.setenv("CURATOR_FEEDBACK_FILE", str(store))

        from curator import feedback_store

        with patch("curator.config.FEEDBACK_DECAY_ENABLED", True):
            feedback_store.apply("viking://test", "adopt")  # first: creates stats_v2
            feedback_store.apply("viking://test", "adopt")  # second: stats_v2_existed=True

        data = feedback_store.load(store)
        rec = data["viking://test"]
        assert rec["adopt"] == 2
        assert rec["stats_v2"]["adopt_w"] == 2.0
        assert rec["stats_v2"]["seen_w"] == 2.0

    def test_decay_factor_computation(self):
        """_decay_factor should return < 1.0 for past timestamps."""
        from datetime import datetime, timedelta, timezone

        from curator.feedback_store import _decay_factor

        past = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        factor = _decay_factor(past, half_life_days=14.0)
        # 14 days at half_life=14 → factor ≈ 0.5
        assert 0.45 < factor < 0.55, f"14-day decay factor should be ~0.5, got {factor}"

    def test_decay_factor_recent_returns_near_one(self):
        """_decay_factor should return ≈ 1.0 for very recent timestamps."""
        from datetime import datetime, timezone

        from curator.feedback_store import _decay_factor

        now = datetime.now(timezone.utc).isoformat()
        factor = _decay_factor(now, half_life_days=14.0)
        assert factor >= 0.9999

    def test_ensure_stats_v2_migration(self):
        """_ensure_stats_v2 should migrate legacy counters."""
        from curator.feedback_store import _ensure_stats_v2

        rec = {"up": 3, "down": 1, "adopt": 2}
        _ensure_stats_v2(rec)

        s = rec["stats_v2"]
        assert s["up_w"] == 3.0
        assert s["down_w"] == 1.0
        assert s["adopt_w"] == 2.0
        assert s["seen_w"] == 6.0  # 3+1+2

    def test_ensure_stats_v2_idempotent(self):
        """_ensure_stats_v2 should not overwrite existing stats_v2."""
        from curator.feedback_store import _ensure_stats_v2

        existing = {
            "up_w": 99.0,
            "down_w": 0.0,
            "adopt_w": 0.0,
            "seen_w": 100.0,
            "last_decay_at": "2026-01-01T00:00:00+00:00",
            "last_event_at": "2026-01-01T00:00:00+00:00",
            "schema_version": 2,
        }
        rec = {"up": 99, "down": 0, "adopt": 0, "stats_v2": existing}
        _ensure_stats_v2(rec)

        assert rec["stats_v2"]["up_w"] == 99.0  # unchanged


class TestRerankWithFeedbackDataParam:
    """Tests for the feedback_data parameter on rerank_with_feedback."""

    def test_feedback_data_skips_load(self):
        """When feedback_data is provided, feedback_store.load() is NOT called."""
        from curator.retrieval_v2 import rerank_with_feedback

        fb = {"b": {"up": 0, "down": 0, "adopt": 5}}
        with patch("curator.feedback_store.load") as mock_load:
            items = _make_items(("a", 0.80), ("b", 0.75))
            result = rerank_with_feedback(items, feedback_data=fb)

        mock_load.assert_not_called()
        # b should still be boosted
        assert result[0]["uri"] == "b"

    def test_feedback_data_none_falls_back_to_load(self):
        """When feedback_data is None, feedback_store.load() IS called."""
        from curator.retrieval_v2 import rerank_with_feedback

        with patch("curator.feedback_store.load", return_value={}) as mock_load:
            items = _make_items(("a", 0.80))
            rerank_with_feedback(items, feedback_data=None)

        mock_load.assert_called_once()

    def test_feedback_data_empty_dict_returns_unchanged(self):
        """Empty feedback_data dict should return items in original order."""
        from curator.retrieval_v2 import rerank_with_feedback

        items = _make_items(("a", 0.9), ("b", 0.7))
        result = rerank_with_feedback(items, feedback_data={})
        assert [r["uri"] for r in result] == ["a", "b"]
        assert result[0]["score"] == 0.9


class TestFeedbackStorePreload:
    """Tests for single feedback_store.load() per pipeline run."""

    def test_feedback_load_called_once_per_pipeline_run(self, monkeypatch):
        """feedback_store.load() should be called exactly once in a full pipeline run."""
        from unittest.mock import MagicMock

        from curator.backend_memory import InMemoryBackend
        from curator.pipeline_v2 import CuratorPipeline

        backend = InMemoryBackend()

        patches = {
            "validate_config": MagicMock(),
            "route_scope": MagicMock(return_value={"domain": "general", "need_fresh": False, "keywords": []}),
            "assess_coverage": MagicMock(return_value=(0.8, False, "local_sufficient")),
            "load_context": MagicMock(return_value=("context", ["viking://a"], "L0")),
            "capture_case": MagicMock(return_value=None),
            "backend_retrieve": MagicMock(
                return_value={
                    "all_items": [{"uri": "a", "score": 0.8, "abstract": "good"}],
                    "all_items_raw": [{"uri": "a", "score": 0.8, "abstract": "good"}],
                    "memories": [],
                    "resources": [{"uri": "a", "score": 0.8, "abstract": "good"}],
                    "skills": [],
                    "query_plan": None,
                }
            ),
        }

        load_mock = MagicMock(return_value={})

        with patch.multiple("curator.pipeline_v2", **patches):
            with patch("curator.feedback_store.load", load_mock):
                pipeline = CuratorPipeline(backend=backend)
                pipeline.run("test query")

        # feedback_store.load() should be called exactly once (preload at entry)
        assert load_mock.call_count == 1
