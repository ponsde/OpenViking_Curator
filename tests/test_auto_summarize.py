"""Tests for L0/L1 auto-summarization in ingest_markdown_v2."""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("OAI_BASE", "http://localhost:8000/v1")
os.environ.setdefault("OAI_KEY", "test-key")


SAMPLE_MD = "# Test\n\nThis is a test document about OpenViking Curator."
MOCK_LLM_RESPONSE = '{"abstract": "OpenViking Curator 知识治理插件。", "overview": "- 检索\\n- 去重\\n- 入库"}'


class TestAutoSummarize(unittest.TestCase):

    def test_returns_empty_when_disabled(self):
        """AUTO_SUMMARIZE=False → _auto_summarize is never called."""
        from curator import review
        with patch.object(review, "AUTO_SUMMARIZE", False):
            # ingest_markdown_v2 should skip summarization entirely
            with patch.object(review, "_auto_summarize") as mock_sum:
                from curator.backend_memory import InMemoryBackend
                backend = InMemoryBackend()
                result = review.ingest_markdown_v2(backend, "Test", SAMPLE_MD)
                mock_sum.assert_not_called()

    def test_returns_empty_when_no_oai_base(self):
        """_auto_summarize returns {} when OAI_BASE is not set."""
        from curator.review import _auto_summarize
        with patch("curator.review.OAI_BASE", ""):
            result = _auto_summarize(SAMPLE_MD, "Test")
        self.assertEqual(result, {})

    def test_parses_llm_json_correctly(self):
        """_auto_summarize extracts abstract and overview from LLM JSON."""
        from curator.review import _auto_summarize
        with patch("curator.review.chat", return_value=MOCK_LLM_RESPONSE):
            with patch("curator.review.OAI_BASE", "http://localhost"):
                result = _auto_summarize(SAMPLE_MD, "Test")
        self.assertEqual(result["abstract"], "OpenViking Curator 知识治理插件。")
        self.assertIn("检索", result["overview"])

    def test_returns_empty_on_llm_failure(self):
        """_auto_summarize returns {} when all models fail (non-blocking)."""
        from curator.review import _auto_summarize
        with patch("curator.review.chat", side_effect=RuntimeError("network error")):
            with patch("curator.review.OAI_BASE", "http://localhost"):
                result = _auto_summarize(SAMPLE_MD, "Test")
        self.assertEqual(result, {})

    def test_returns_empty_on_bad_json(self):
        """_auto_summarize returns {} when LLM returns non-JSON."""
        from curator.review import _auto_summarize
        with patch("curator.review.chat", return_value="sorry, I cannot summarize"):
            with patch("curator.review.OAI_BASE", "http://localhost"):
                result = _auto_summarize(SAMPLE_MD, "Test")
        self.assertEqual(result, {})

    def test_ingest_with_summarize_injects_abstract_header(self):
        """When AUTO_SUMMARIZE=True and LLM succeeds, abstract appears in curator_meta header."""
        from curator import review
        from curator.backend_memory import InMemoryBackend
        backend = InMemoryBackend()

        captured = {}
        original_ingest = backend.ingest
        def capturing_ingest(content, title="", metadata=None):
            captured["content"] = content
            captured["metadata"] = metadata
            return original_ingest(content, title=title, metadata=metadata)
        backend.ingest = capturing_ingest

        with patch.object(review, "AUTO_SUMMARIZE", True):
            with patch.object(review, "_auto_summarize", return_value={
                "abstract": "Test abstract.", "overview": "- point 1\n- point 2"
            }):
                review.ingest_markdown_v2(backend, "Test Doc", SAMPLE_MD)

        self.assertIn("<!-- abstract: Test abstract. -->", captured["content"])
        self.assertIn("## 摘要", captured["content"])
        self.assertIn("- point 1", captured["content"])
        self.assertEqual(captured["metadata"].get("abstract"), "Test abstract.")

    def test_ingest_without_summarize_no_abstract_header(self):
        """When AUTO_SUMMARIZE=False, no abstract comment in content."""
        from curator import review
        from curator.backend_memory import InMemoryBackend
        backend = InMemoryBackend()
        captured = {}
        original_ingest = backend.ingest
        def capturing_ingest(content, title="", metadata=None):
            captured["content"] = content
            captured["metadata"] = metadata
            return original_ingest(content, title=title, metadata=metadata)
        backend.ingest = capturing_ingest

        with patch.object(review, "AUTO_SUMMARIZE", False):
            review.ingest_markdown_v2(backend, "Test Doc", SAMPLE_MD)

        self.assertNotIn("<!-- abstract:", captured.get("content", ""))
        self.assertNotIn("## 摘要", captured.get("content", ""))

    def test_ingest_summarize_failure_still_ingests(self):
        """If _auto_summarize fails, ingest_markdown_v2 still proceeds normally."""
        from curator import review
        from curator.backend_memory import InMemoryBackend
        backend = InMemoryBackend()

        with patch.object(review, "AUTO_SUMMARIZE", True):
            with patch.object(review, "_auto_summarize", return_value={}):
                result = review.ingest_markdown_v2(backend, "Test", SAMPLE_MD)

        # Should succeed even with empty summary
        self.assertIn("root_uri", result)

    def test_abstract_html_comment_sanitized(self):
        """Abstract containing '-->' is sanitized before embedding in HTML comment."""
        from curator import review
        from curator.backend_memory import InMemoryBackend
        backend = InMemoryBackend()

        captured = {}
        original_ingest = backend.ingest
        def capturing_ingest(content, title="", metadata=None):
            captured["content"] = content
            captured["metadata"] = metadata
            return original_ingest(content, title=title, metadata=metadata)
        backend.ingest = capturing_ingest

        # abstract 含 '-->' —— 如果不 sanitize，会提前关闭 HTML comment
        dangerous_abstract = "A --> B 关系，见图表"
        with patch.object(review, "AUTO_SUMMARIZE", True):
            with patch.object(review, "_auto_summarize", return_value={
                "abstract": dangerous_abstract, "overview": ""
            }):
                review.ingest_markdown_v2(backend, "Test Doc", SAMPLE_MD)

        content = captured.get("content", "")
        # HTML comment 里不能有 -->（会提前关闭注释）
        self.assertNotIn("A --> B", content)
        # 替换后用 → 存入
        self.assertIn("<!-- abstract:", content)
        self.assertIn("→", content)
        # metadata 里同样不含原始 -->
        self.assertNotIn("-->", captured["metadata"].get("abstract", ""))


if __name__ == "__main__":
    unittest.main()
