"""Interest Analyzer: extract user interests from query_log + feedback.

Generates proactive search queries for topics where coverage is weak but
user interest is high.  All scoring is rule-based (0 LLM calls).  An
optional LLM path can improve query generation quality when configured.

Data sources:
- ``data/query_log.jsonl`` — what users queried, coverage, external triggers
- ``data/feedback.json`` — adopt/up/down signals per URI
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import DATA_PATH, env, log
from .feedback_store import load as load_feedback
from .nlp_utils import extract_keywords
from .nlp_utils import extract_topic_coarse as _coarse_topic


@dataclass(frozen=True)
class InterestTopic:
    """A topic with measured user interest."""

    topic: str
    query_count: int
    avg_coverage: float
    adopt_score: int
    interest_score: float
    sample_queries: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProactiveQuery:
    """A search query to proactively fill a knowledge gap."""

    query: str
    topic: str
    reason: str  # e.g. "high_interest_low_coverage"


def _load_query_log(
    data_path: str,
    lookback_days: int,
) -> list[dict]:
    """Load query_log.jsonl entries within the lookback window."""
    log_path = os.path.join(data_path, "query_log.jsonl")
    if not os.path.exists(log_path):
        return []

    cutoff = time.time() - lookback_days * 86400
    entries: list[dict] = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Filter by time window
            ts_str = entry.get("timestamp", "")
            if ts_str:
                try:
                    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if dt.timestamp() < cutoff:
                        continue
                except (ValueError, TypeError):
                    pass
            entries.append(entry)
    return entries


def _compute_adopt_by_topic(
    feedback: dict,
    entries: list[dict],
) -> dict[str, int]:
    """Map topic → total adopt score from feedback on related URIs."""
    # Build topic → URIs mapping from query log
    topic_uris: dict[str, set[str]] = {}
    for entry in entries:
        topic = _coarse_topic(entry.get("query", ""))
        for uri in entry.get("used_uris", []):
            topic_uris.setdefault(topic, set()).add(uri)

    # Sum adopt counts for each topic's URIs
    result: dict[str, int] = {}
    for topic, uris in topic_uris.items():
        total = 0
        for uri in uris:
            fb = feedback.get(uri, {})
            total += fb.get("adopt", 0)
        result[topic] = total
    return result


def extract_interests(
    data_path: str | None = None,
    lookback_days: int = 30,
    min_queries: int = 2,
    max_topics: int = 20,
) -> list[InterestTopic]:
    """Extract user interest topics from query log and feedback.

    Scoring formula (all weights sum to 1.0):
        0.4 * frequency_norm + 0.3 * (1 - avg_coverage) + 0.2 * adopt_norm + 0.1 * recency_norm

    Args:
        data_path:      Path to data directory (default: DATA_PATH).
        lookback_days:  How many days back to analyze.
        min_queries:    Minimum queries on a topic to be considered.
        max_topics:     Maximum topics to return.

    Returns:
        List of InterestTopic sorted by interest_score descending.
    """
    _data_path = data_path or DATA_PATH
    entries = _load_query_log(_data_path, lookback_days)
    if not entries:
        return []

    feedback = load_feedback()
    adopt_by_topic = _compute_adopt_by_topic(feedback, entries)

    # Group by topic
    now = time.time()
    topic_data: dict[str, dict] = {}
    for entry in entries:
        topic = _coarse_topic(entry.get("query", ""))
        if topic not in topic_data:
            topic_data[topic] = {
                "coverages": [],
                "queries": [],
                "timestamps": [],
                "count": 0,
            }
        td = topic_data[topic]
        td["coverages"].append(float(entry.get("coverage") or 0.0))
        td["queries"].append(entry.get("query", ""))
        td["count"] += 1
        # Parse timestamp for recency
        ts_str = entry.get("timestamp", "")
        if ts_str:
            try:
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                td["timestamps"].append(dt.timestamp())
            except (ValueError, TypeError):
                pass

    # Filter by min_queries
    filtered = {t: d for t, d in topic_data.items() if d["count"] >= min_queries}
    if not filtered:
        return []

    # Compute normalization bounds
    max_count = max(d["count"] for d in filtered.values())
    max_adopt = max((adopt_by_topic.get(t, 0) for t in filtered), default=1) or 1

    results: list[InterestTopic] = []
    for topic, data in filtered.items():
        avg_cov = sum(data["coverages"]) / data["count"]
        adopt = adopt_by_topic.get(topic, 0)

        # Frequency: normalized 0-1
        freq_norm = data["count"] / max_count if max_count > 0 else 0

        # Coverage gap: higher = worse coverage = more interesting
        cov_gap = 1.0 - avg_cov

        # Adopt: normalized 0-1
        adopt_norm = adopt / max_adopt if max_adopt > 0 else 0

        # Recency: how recent is the latest query (0-1, 1 = very recent)
        if data["timestamps"]:
            latest = max(data["timestamps"])
            age_days = (now - latest) / 86400
            recency_norm = max(0.0, 1.0 - age_days / lookback_days)
        else:
            recency_norm = 0.5

        score = 0.4 * freq_norm + 0.3 * cov_gap + 0.2 * adopt_norm + 0.1 * recency_norm

        # Deduplicate sample queries
        seen: set[str] = set()
        samples: list[str] = []
        for q in data["queries"]:
            if q not in seen and len(samples) < 3:
                seen.add(q)
                samples.append(q)

        results.append(
            InterestTopic(
                topic=topic,
                query_count=data["count"],
                avg_coverage=round(avg_cov, 4),
                adopt_score=adopt,
                interest_score=round(score, 4),
                sample_queries=tuple(samples),
            )
        )

    results.sort(key=lambda t: -t.interest_score)
    return results[:max_topics]


def _generate_queries_rule_based(
    interests: list[InterestTopic],
    existing_queries: set[str],
    max_queries: int,
) -> list[ProactiveQuery]:
    """Generate proactive queries using rule-based keyword expansion."""
    current_year = str(datetime.now(timezone.utc).year)
    suffixes = ["latest best practices", current_year, "common issues and solutions"]
    zh_suffixes = ["最新进展", "最佳实践", "常见问题"]

    queries: list[ProactiveQuery] = []
    for interest in interests:
        if len(queries) >= max_queries:
            break
        # Skip high-coverage topics
        if interest.avg_coverage > 0.7:
            continue

        topic = interest.topic
        # Detect language: if topic has CJK chars, use Chinese suffixes
        has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in topic)
        suf_list = zh_suffixes if has_cjk else suffixes

        for suffix in suf_list:
            candidate = f"{topic} {suffix}"
            if candidate.lower() in existing_queries:
                continue
            queries.append(
                ProactiveQuery(
                    query=candidate,
                    topic=topic,
                    reason="high_interest_low_coverage",
                )
            )
            break  # One query per topic

    return queries[:max_queries]


def _generate_queries_llm(
    interests: list[InterestTopic],
    existing_queries: set[str],
    max_queries: int,
) -> list[ProactiveQuery]:
    """Generate proactive queries using LLM for better quality.

    Falls back to rule-based if LLM call fails.
    """
    from .config import OAI_BASE, OAI_KEY, ROUTER_MODELS, chat

    if not OAI_BASE or not OAI_KEY or not ROUTER_MODELS:
        log.debug("interest_analyzer: LLM not configured, falling back to rules")
        return _generate_queries_rule_based(interests, existing_queries, max_queries)

    # Build compact prompt
    topic_lines = []
    for t in interests[:10]:
        if t.avg_coverage > 0.7:
            continue
        topic_lines.append(f"- {t.topic} (queries={t.query_count}, coverage={t.avg_coverage:.2f})")

    if not topic_lines:
        return []

    prompt = (
        "You are a knowledge curation assistant. Given these topics where our "
        "knowledge base has weak coverage, generate search queries to fill the gaps.\n\n"
        "Topics with weak coverage:\n" + "\n".join(topic_lines) + f"\n\nGenerate up to {max_queries} search queries. "
        "Each query should be specific and actionable. "
        "Output as JSON array of objects with 'query' and 'topic' fields.\n"
        'Example: [{"query": "Redis cluster failover best practices 2026", "topic": "redis cluster"}]'
    )

    try:
        resp = chat(
            OAI_BASE,
            OAI_KEY,
            ROUTER_MODELS[0],
            [{"role": "user", "content": prompt}],
            timeout=30,
            temperature=0.3,
        )
        # Parse JSON array from response (try each match, prefer valid JSON)
        import re

        items = None
        for match in re.finditer(r"\[.*?\]", resp, re.DOTALL):
            try:
                candidate = json.loads(match.group())
                if isinstance(candidate, list) and candidate:
                    items = candidate
                    break
            except json.JSONDecodeError:
                continue

        if items is None:
            log.debug("interest_analyzer: LLM response not parseable, falling back")
            return _generate_queries_rule_based(interests, existing_queries, max_queries)
        queries: list[ProactiveQuery] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            q = item.get("query", "").strip()
            tp: str = item.get("topic", "").strip()
            if not q or q.lower() in existing_queries:
                continue
            queries.append(ProactiveQuery(query=q, topic=tp, reason="llm_generated"))
            if len(queries) >= max_queries:
                break
        return queries

    except Exception as e:
        log.warning("interest_analyzer: LLM query generation failed: %s, falling back", e)
        return _generate_queries_rule_based(interests, existing_queries, max_queries)


def generate_proactive_queries(
    interests: list[InterestTopic],
    existing_queries: set[str] | None = None,
    max_queries: int = 5,
    use_llm: bool = False,
) -> list[ProactiveQuery]:
    """Generate proactive search queries for high-interest low-coverage topics.

    Args:
        interests:        Output of extract_interests().
        existing_queries: Queries already run recently (for dedup).
        max_queries:      Maximum queries to generate.
        use_llm:          If True, use LLM for better query quality (1 call).

    Returns:
        List of ProactiveQuery objects.
    """
    _existing = {q.lower() for q in (existing_queries or set())}

    if use_llm:
        return _generate_queries_llm(interests, _existing, max_queries)
    return _generate_queries_rule_based(interests, _existing, max_queries)
