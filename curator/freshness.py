"""Freshness: 时效评分（OV 没有语义层时效衰减，这是 Curator 的补充）。

用途：
1. 入库时打时效标签（ingest_markdown_v2）
2. 后台过期扫描（freshness_rescan.py）

不用于：重新排序 OV 检索结果。
"""

import re
import time


def _extract_timestamp_from_uri(uri: str) -> int | None:
    """Extract Unix timestamp from URI path (e.g. viking://resources/1771327401_xxx)."""
    m = re.search(r'/(\d{10})_', uri or '')
    if m:
        return int(m.group(1))
    return None


def uri_freshness_score(uri: str, meta: dict | None = None, now: float | None = None) -> float:
    """Freshness scoring based on document age.

    Scoring logic:
    - Documents < 30 days old: 1.0 (full freshness)
    - 30-180 days: linear decay from 1.0 to 0.5
    - 180-365 days: linear decay from 0.5 to 0.2
    - > 365 days: 0.1 (very stale)

    Sources (in priority order):
    1. meta['review_after'] or meta['created_at'] (ISO date or Unix timestamp)
    2. URI embedded timestamp (e.g. /1771327401_xxx)
    3. Fallback: 0.5 (unknown age)
    """
    _now = now or time.time()
    doc_ts = None

    # ── Try meta dates ──
    if meta and isinstance(meta, dict):
        for key in ('review_after', 'created_at', 'ingested_at', 'date'):
            val = meta.get(key)
            if not val:
                continue
            if isinstance(val, (int, float)) and val > 1_000_000_000:
                doc_ts = float(val)
                break
            if isinstance(val, str):
                try:
                    import datetime
                    dt = datetime.datetime.fromisoformat(val.replace('Z', '+00:00'))
                    doc_ts = dt.timestamp()
                    break
                except (ValueError, TypeError):
                    pass

    # ── Try URI timestamp ──
    if doc_ts is None:
        doc_ts = _extract_timestamp_from_uri(uri)

    # ── Unknown age fallback ──
    if doc_ts is None:
        return 0.5

    age_days = (_now - doc_ts) / 86400

    if age_days < 0:
        return 1.0  # future date, treat as fresh
    if age_days <= 30:
        return 1.0
    if age_days <= 180:
        return round(1.0 - 0.5 * (age_days - 30) / 150, 3)
    if age_days <= 365:
        return round(0.5 - 0.3 * (age_days - 180) / 185, 3)
    return 0.1
