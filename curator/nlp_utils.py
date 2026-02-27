"""Shared NLP utilities: keyword extraction, topic extraction, stopwords.

Used by:
- ``interest_analyzer.py`` (topic grouping for governance)
- ``scripts/analyze_weak.py`` (weak topic detection)
- ``governance.py`` (phase 1 data collection)
- ``scheduler.py`` (strengthen job — inline weak topic analysis)
"""

from __future__ import annotations

import json
import os
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


def analyze_weak_topics(data_path: str, min_queries: int = 2) -> list[dict]:
    """Analyse query_log.jsonl and return weak topics sorted by severity.

    A topic is "weak" when its external_rate > 0.5 (more than half of queries
    needed external search) and it has been queried at least *min_queries* times.

    Args:
        data_path:   Directory containing ``query_log.jsonl``.
        min_queries: Minimum query count to be considered (filters noise).

    Returns:
        List of dicts sorted by ``(-external_rate, -query_count)``:
        ``{"topic", "query_count", "avg_coverage", "external_rate"}``
    """
    from collections import defaultdict

    log_path = os.path.join(data_path, "query_log.jsonl")
    if not os.path.exists(log_path):
        return []

    entries: list[dict] = []
    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []

    if not entries:
        return []

    topic_stats: dict[str, dict] = defaultdict(lambda: {"coverages": [], "external_count": 0, "total": 0})
    for entry in entries:
        topic = extract_topic(entry.get("query", ""))
        stats = topic_stats[topic]
        stats["coverages"].append(float(entry.get("coverage") or 0.0))
        if entry.get("external_triggered", False):
            stats["external_count"] += 1
        stats["total"] += 1

    weak: list[dict] = []
    for topic, stats in topic_stats.items():
        count = stats["total"]
        avg_cov = sum(stats["coverages"]) / count if count else 0.0
        ext_rate = stats["external_count"] / count if count else 0.0
        if ext_rate > 0.5 and count >= min_queries:
            weak.append(
                {
                    "topic": topic,
                    "query_count": count,
                    "avg_coverage": round(avg_cov, 4),
                    "external_rate": round(ext_rate, 4),
                }
            )

    weak.sort(key=lambda x: (-x["external_rate"], -x["query_count"]))
    return weak
