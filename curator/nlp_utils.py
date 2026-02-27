"""Shared NLP utilities: keyword extraction, topic extraction, stopwords.

Used by:
- ``interest_analyzer.py`` (topic grouping for governance)
- ``scripts/analyze_weak.py`` (weak topic detection)
- ``governance.py`` (phase 1 data collection)
"""

from __future__ import annotations

import re

# Stopwords (Chinese + English common function words)
STOP_WORDS = {
    "的",
    "了",
    "在",
    "是",
    "我",
    "有",
    "和",
    "就",
    "不",
    "人",
    "都",
    "一",
    "一个",
    "上",
    "也",
    "很",
    "到",
    "说",
    "要",
    "去",
    "你",
    "会",
    "着",
    "没有",
    "看",
    "好",
    "自己",
    "这",
    "他",
    "她",
    "它",
    "们",
    "那",
    "些",
    "什么",
    "怎么",
    "如何",
    "为什么",
    "哪些",
    "吗",
    "呢",
    "吧",
    "啊",
    "the",
    "a",
    "an",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "have",
    "has",
    "had",
    "do",
    "does",
    "did",
    "will",
    "would",
    "shall",
    "should",
    "may",
    "might",
    "can",
    "could",
    "of",
    "in",
    "to",
    "for",
    "with",
    "on",
    "at",
    "from",
    "by",
    "about",
    "as",
    "into",
    "through",
    "and",
    "or",
    "but",
    "not",
    "no",
    "so",
    "if",
    "than",
    "too",
    "very",
    "how",
    "what",
    "which",
    "who",
    "when",
    "where",
    "why",
    "this",
    "that",
    "it",
    "i",
    "me",
    "my",
    "we",
    "our",
    "you",
    "your",
    "he",
    "she",
}


def extract_keywords(query: str) -> list[str]:
    """Extract keywords from a query (simple tokenization + stopword filter).

    Handles mixed Chinese/English text. Chinese text is kept as word groups
    (not split into individual characters).
    """
    tokens = re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9_\-\.]+", query.lower())
    return [t for t in tokens if t not in STOP_WORDS and len(t) > 1]


def extract_topic(query: str) -> str:
    """Extract a topic slug from a query (top 3 keywords joined).

    For coarser grouping, use ``extract_topic_coarse`` (top 2 keywords).
    """
    kws = extract_keywords(query)
    if not kws:
        return query.strip()[:30] or "unknown"
    return " ".join(kws[:3])


def extract_topic_coarse(query: str) -> str:
    """Extract a coarse topic (top 2 keywords) for broader grouping.

    Groups "docker compose setup" and "docker compose networking" into
    the same bucket "docker compose".
    """
    kws = extract_keywords(query)
    if not kws:
        return query.strip()[:30] or "unknown"
    return " ".join(kws[:2])
