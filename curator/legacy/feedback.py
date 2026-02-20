"""Feedback scoring: trust, freshness, feedback-weighted URI ranking."""

import json
import re
import time
from pathlib import Path


def load_feedback(path: str):
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            return {}
    return {}


def uri_feedback_score(uri: str, fb: dict) -> int:
    if not isinstance(fb, dict):
        return 0

    def _score(item):
        up = int(item.get('up', 0))
        down = int(item.get('down', 0))
        adopt = int(item.get('adopt', 0))
        return up - down + adopt * 2

    # exact match
    if uri in fb:
        return _score(fb[uri])

    # fuzzy match: same subtree / parent-child path overlap
    best = 0
    for k, v in fb.items():
        if not isinstance(k, str):
            continue
        if k in uri or uri in k:
            best = max(best, _score(v))
    return best


def uri_trust_score(uri: str, fb: dict | None = None) -> float:
    """Trust scoring: URL heuristic + feedback-weighted adjustment.

    Base score from URL pattern (project relevance), then adjusted by
    feedback signals (up/down/adopt) if feedback data is provided.

    Score range: 1.0 ~ 10.0
    """
    u = (uri or "").lower()

    # ── Base score from URL pattern ──
    base = 5.5  # default for unknown URIs
    if 'openviking' in u or 'grok2api' in u or 'newapi' in u:
        base = 7.0
    elif 'curated' in u or 'single_' in u or 'reingest_' in u:
        base = 6.5
    elif any(tag in u for tag in ('memory', 'case', 'experience')):
        base = 6.0
    elif 'license' in u or 'readme' in u:
        base = 4.0
    elif 'tmp' in u or 'temp' in u:
        base = 3.0

    # ── Feedback adjustment ──
    if fb and isinstance(fb, dict):
        fb_score = uri_feedback_score(uri, fb)
        # Each net feedback point adjusts trust by 0.3, capped at ±2.0
        fb_adj = max(-2.0, min(2.0, fb_score * 0.3))
        base += fb_adj

    return max(1.0, min(10.0, round(base, 2)))


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
                # Try ISO date parse (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)
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
        # Linear decay: 1.0 → 0.5 over 150 days
        return round(1.0 - 0.5 * (age_days - 30) / 150, 3)
    if age_days <= 365:
        # Linear decay: 0.5 → 0.2 over 185 days
        return round(0.5 - 0.3 * (age_days - 180) / 185, 3)
    return 0.1


def build_feedback_priority_uris(uris, feedback_file='feedback.json', topn=3):
    fb = load_feedback(feedback_file)
    scored = []
    seen = set()
    for u in uris:
        if u in seen:
            continue
        seen.add(u)
        f = uri_feedback_score(u, fb)
        t = uri_trust_score(u, fb)
        r = uri_freshness_score(u)
        final = 0.50 * f + 0.30 * t + 0.20 * r
        scored.append((final, f, t, r, u))
    scored.sort(reverse=True, key=lambda x: x[0])
    return [x[4] for x in scored[:topn]], scored[:min(5, len(scored))]
