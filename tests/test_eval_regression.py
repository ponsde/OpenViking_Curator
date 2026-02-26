"""Eval regression: fixed query set against InMemoryBackend.

Asserts pipeline decisions (routing, coverage, external trigger, etc.)
are stable across commits. No network, no LLM — pure deterministic.

Run:
    python -m pytest tests/test_eval_regression.py -v
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("OAI_BASE", "http://localhost:8000/v1")
os.environ.setdefault("OAI_KEY", "test-key")

# ── Seeded knowledge base ──
# Each entry simulates a curated resource already in the knowledge base.
SEED_KNOWLEDGE = [
    {
        "content": (
            "# grok2api 部署指南\n"
            "grok2api 是 Grok API 的反向代理，默认监听端口 8000。\n"
            "配置要点：设置 API_KEY 环境变量，启动后通过 /v1/chat/completions 调用。\n"
            "常见问题：端口冲突、TLS 证书路径错误、代理超时。"
        ),
        "title": "grok2api-deploy",
        "metadata": {"domain": "ops"},
    },
    {
        "content": (
            "# OpenViking search vs find\n"
            "search 使用向量检索（非确定性，受嵌入模型影响），find 使用 Jaccard 相似度。\n"
            "两者结果不一致是正常现象。缓解方法：合并去重、设置最低阈值。\n"
            "本地索引重建后结果可能变化。"
        ),
        "title": "ov-search-find-diff",
        "metadata": {"domain": "knowledge"},
    },
    {
        "content": (
            "# Curator 路由设计\n"
            "初版使用正则规则匹配 domain。升级方案：用 LLM 做意图识别，\n"
            "但保留硬拦截规则（安全域、敏感词）。当前用 Grok 做 fast route。\n"
            "规则优先级：硬拦截 > LLM 意图 > 默认 general。"
        ),
        "title": "curator-router-design",
        "metadata": {"domain": "architecture"},
    },
    {
        "content": (
            "# SSE 流式响应问题\n"
            "grok2api 默认 stream=true，返回 SSE 格式。某些客户端把 SSE chunk\n"
            "当完整 JSON 解析导致失败。修复：设置 stream=false 或客户端正确拼接 chunk。"
        ),
        "title": "sse-json-parse-fix",
        "metadata": {"domain": "bugfix"},
    },
    {
        "content": (
            "# 交叉验证策略\n"
            "cross_validate 在外搜结果入库前执行，对比本地知识和外部来源。\n"
            "检测冲突点，给出冲突摘要。冲突严重时写入 pending_review 等人工确认。\n"
            "目的：防止不准确内容污染知识库。"
        ),
        "title": "cross-validate-strategy",
        "metadata": {"domain": "strategy"},
    },
    {
        "content": (
            "# URI 信任度评分\n"
            "freshness: current/recent/stale/outdated 四档，基于入库时间衰减。\n"
            "trust: 0-10 整数，judge LLM 打分。feedback 记录 adopt/up/down，\n"
            "通过 rerank_with_feedback 微调 score delta ∈ (-0.10, +0.10)。"
        ),
        "title": "uri-trust-scoring",
        "metadata": {"domain": "algorithm"},
    },
]

# ── Fixed query set with expected behavior ──
# NOTE: InMemoryBackend uses simple keyword matching, not vector similarity.
# Scores are inherently low (~0.05-0.10). These assertions test behavioral
# stability (regression detection), not absolute quality metrics.
EVAL_QUERIES = [
    {
        "id": "Q1",
        "query": "grok2api 怎么部署",
        "expect": {
            # In-domain: seeded content matches → has context
            "context_nonempty": True,
            "has_results": True,
        },
    },
    {
        "id": "Q2",
        "query": "OpenViking search 和 find 有什么区别",
        "expect": {
            "context_nonempty": True,
            "has_results": True,
        },
    },
    {
        "id": "Q3",
        "query": "量子计算在密码学中的最新进展",
        "expect": {
            # No seeded knowledge on this topic
            "external_triggered": True,
        },
    },
    {
        "id": "Q4",
        "query": "SSE 流式解析 JSON 失败",
        "expect": {
            "context_nonempty": True,
            "has_results": True,
        },
    },
    {
        "id": "Q5",
        "query": "curator 怎么做交叉验证",
        "expect": {
            "context_nonempty": True,
            "has_results": True,
        },
    },
    {
        "id": "Q6",
        "query": "Rust borrow checker 生命周期标注最佳实践",
        "expect": {
            # Completely out-of-domain
            "external_triggered": True,
        },
    },
]


def _mock_judge(*a, **kw):
    """Deterministic judge result for eval."""
    return {
        "pass": True,
        "trust": 7,
        "freshness": "current",
        "reason": "eval mock",
        "markdown": "# Eval Mock Result",
        "has_conflict": False,
        "conflict_summary": "",
        "conflict_points": [],
    }


def _make_backend_retrieve_from_backend(backend):
    """Build a backend_retrieve mock that queries InMemoryBackend instead of real backend."""

    def _mock_backend_retrieve(bk, query, session_id=None, limit=10):
        find_result = backend.find(query, limit=limit)
        resources = []
        for r in find_result.results:
            resources.append(
                {
                    "uri": r.uri,
                    "score": r.score,
                    "abstract": (r.abstract or "")[:100],
                    "raw_score": r.score,
                }
            )
        return {
            "memories": [],
            "resources": resources,
            "skills": [],
            "query_plan": None,
            "all_items": resources,
            "all_items_raw": resources,
        }

    return _mock_backend_retrieve


class TestEvalRegression(unittest.TestCase):
    """Pipeline behavior regression against fixed query set."""

    @classmethod
    def setUpClass(cls):
        from curator.backend_memory import InMemoryBackend

        cls.backend = InMemoryBackend()
        for seed in SEED_KNOWLEDGE:
            cls.backend.ingest(seed["content"], title=seed["title"], metadata=seed["metadata"])

    def _run_query(self, query: str) -> dict:
        """Run a single query through the pipeline with mocked externals."""
        patches = {
            "backend_retrieve": MagicMock(side_effect=_make_backend_retrieve_from_backend(self.backend)),
            "external_search": MagicMock(return_value="External search mock content"),
            "cross_validate": MagicMock(return_value={"validated": "mock", "warnings": []}),
            "judge_and_ingest": MagicMock(side_effect=_mock_judge),
            "capture_case": MagicMock(return_value=None),
            "validate_config": MagicMock(),
        }

        with patch.multiple("curator.pipeline_v2", **patches):
            from curator.pipeline_v2 import run

            return run(query, backend=self.backend)

    def test_seeded_knowledge_loaded(self):
        """Verify backend has all seeded entries."""
        for seed in SEED_KNOWLEDGE:
            results = self.backend.find(seed["title"], limit=3)
            self.assertTrue(
                len(results.results) > 0,
                f"Seed '{seed['title']}' should be findable in backend",
            )

    def test_eval_queries(self):
        """Run all eval queries and assert expected behavior."""
        failures = []

        for eq in EVAL_QUERIES:
            qid = eq["id"]
            result = self._run_query(eq["query"])
            expect = eq["expect"]

            ext_triggered = result.get("meta", {}).get("external_triggered", False)
            has_context = len(result.get("context_text", "").strip()) > 0
            has_results = len(result.get("ov_results", {}).get("resources", [])) > 0

            checks = []

            if ext_triggered != expect.get("external_triggered", ext_triggered):
                checks.append(f"external_triggered={ext_triggered}, expected={expect['external_triggered']}")
            if has_context != expect.get("context_nonempty", has_context):
                checks.append(f"context_nonempty={has_context}, expected={expect['context_nonempty']}")
            if has_results != expect.get("has_results", has_results):
                checks.append(f"has_results={has_results}, expected={expect['has_results']}")

            if checks:
                failures.append(f"  {qid} ({eq['query'][:30]}...): {'; '.join(checks)}")

        if failures:
            self.fail(f"Eval regression failures ({len(failures)}/{len(EVAL_QUERIES)}):\n" + "\n".join(failures))

    def test_coverage_ordering(self):
        """Queries with relevant seeded data should score higher than out-of-domain."""
        in_domain = self._run_query("grok2api 怎么部署")
        out_domain = self._run_query("量子计算在密码学中的最新进展")

        self.assertGreater(
            in_domain["coverage"],
            out_domain["coverage"],
            "In-domain query should have higher coverage than out-of-domain",
        )

    def test_no_pipeline_errors(self):
        """All eval queries should complete without errors in meta."""
        for eq in EVAL_QUERIES:
            result = self._run_query(eq["query"])
            error = result.get("meta", {}).get("error")
            self.assertIsNone(
                error,
                f"{eq['id']}: pipeline error: {error}",
            )

    def test_decision_report_generated(self):
        """Every run should produce a non-empty decision report."""
        result = self._run_query("grok2api 怎么部署")
        report = result.get("decision_report", "")
        self.assertTrue(len(report) > 0, "decision_report should be non-empty")

    def test_conflict_blocks_ingest_writes_pending(self):
        """When judge returns conflict preferred=human_review, ingest is blocked and pending is written."""

        def _conflict_judge(*a, **kw):
            return {
                "pass": True,
                "trust": 5,
                "freshness": "current",
                "reason": "conflict detected",
                "markdown": "# Conflicting Content",
                "has_conflict": True,
                "conflict_summary": "Local says X, external says Y",
                "conflict_points": ["point A"],
            }

        with tempfile.TemporaryDirectory() as tmpdir:
            patches = {
                "backend_retrieve": MagicMock(side_effect=_make_backend_retrieve_from_backend(self.backend)),
                "assess_coverage": MagicMock(return_value=(0.2, True, "low_coverage")),
                "external_search": MagicMock(return_value="External conflict content"),
                "cross_validate": MagicMock(return_value={"validated": "mock", "warnings": []}),
                "judge_and_ingest": MagicMock(side_effect=_conflict_judge),
                "capture_case": MagicMock(return_value=None),
                "validate_config": MagicMock(),
            }

            with patch("curator.pipeline_v2.DATA_PATH", tmpdir):
                with patch.multiple("curator.pipeline_v2", **patches):
                    from curator.pipeline_v2 import run

                    result = run("grok2api 部署", backend=self.backend)

            # Conflict should be detected
            self.assertTrue(result["conflict"]["has_conflict"])
            # Ingest should NOT have happened (blocked by conflict)
            self.assertFalse(result["meta"]["ingested"])
            # Pending review file should be written
            pending = Path(tmpdir) / "pending_review.jsonl"
            self.assertTrue(pending.exists(), "pending_review.jsonl should be written for conflict")
            entry = json.loads(pending.read_text().strip().split("\n")[-1])
            self.assertIn("conflict", entry["reason"])

    def test_need_fresh_triggers_cross_validate(self):
        """When scope has need_fresh=True, cross_validate should be called."""
        cv_called = False

        def _tracking_cv(query, text, scope):
            nonlocal cv_called
            cv_called = True
            return {"validated": text, "warnings": ["stale warning"]}

        def _mock_route(q):
            return {"domain": "tech", "need_fresh": True}

        patches = {
            "backend_retrieve": MagicMock(side_effect=_make_backend_retrieve_from_backend(self.backend)),
            "external_search": MagicMock(return_value="External fresh content"),
            "cross_validate": MagicMock(side_effect=_tracking_cv),
            "judge_and_ingest": MagicMock(side_effect=_mock_judge),
            "route_scope": MagicMock(side_effect=_mock_route),
            "capture_case": MagicMock(return_value=None),
            "validate_config": MagicMock(),
        }

        with patch.multiple("curator.pipeline_v2", **patches):
            from curator.pipeline_v2 import run

            result = run("latest grok2api changes", backend=self.backend)

        self.assertTrue(cv_called, "cross_validate should be called when need_fresh=True")
        self.assertIn("stale warning", result["meta"].get("warnings", []))

    def test_async_ingest_pending_flag_combinations(self):
        """async_ingest_pending in meta should follow ASYNC_INGEST × auto_ingest matrix."""
        patches = {
            "backend_retrieve": MagicMock(side_effect=_make_backend_retrieve_from_backend(self.backend)),
            "assess_coverage": MagicMock(return_value=(0.2, True, "low_coverage")),
            "external_search": MagicMock(return_value="External content"),
            "cross_validate": MagicMock(return_value={"validated": "mock", "warnings": []}),
            "judge_and_ingest": MagicMock(side_effect=_mock_judge),
            "capture_case": MagicMock(return_value=None),
            "validate_config": MagicMock(),
        }

        cases = [
            # (async_env, auto_ingest, expected_async_pending)
            ("0", True, False),  # async off → sync judge
            ("0", False, False),  # async off + review mode → sync judge
            ("1", True, True),  # async on + auto → deferred
            ("1", False, False),  # async on + review mode → sync (user needs result)
        ]

        for async_env, auto_ingest, expected_pending in cases:
            with self.subTest(async_env=async_env, auto_ingest=auto_ingest):
                with patch.dict(os.environ, {"CURATOR_ASYNC_INGEST": async_env}):
                    with patch.multiple("curator.pipeline_v2", **patches):
                        from curator.pipeline_v2 import run

                        result = run("grok2api 部署", backend=self.backend, auto_ingest=auto_ingest)

                actual = result["meta"].get("async_ingest_pending", False)
                self.assertEqual(
                    actual,
                    expected_pending,
                    f"ASYNC={async_env}, auto_ingest={auto_ingest}: "
                    f"expected async_pending={expected_pending}, got {actual}",
                )


if __name__ == "__main__":
    unittest.main()
