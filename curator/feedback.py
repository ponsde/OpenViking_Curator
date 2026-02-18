"""Feedback scoring: trust, freshness, feedback-weighted URI ranking."""

import json
import re
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


def uri_trust_score(uri: str) -> float:
    """Simple URL-based trust heuristic. TODO: replace with real trust scoring."""
    u = (uri or "").lower()
    if 'openviking' in u or 'grok2api' in u or 'newapi' in u:
        return 7.0
    if 'curated' in u:
        return 6.5
    if 'license' in u or 'readme' in u:
        return 4.0
    return 5.5


def uri_freshness_score(uri: str) -> float:
    """Freshness heuristic. TODO: implement real date-based scoring."""
    # Placeholder: always returns 1.0
    # Future: parse curator_meta from document, check review_after date
    return 1.0


def build_feedback_priority_uris(uris, feedback_file='feedback.json', topn=3):
    fb = load_feedback(feedback_file)
    scored = []
    seen = set()
    for u in uris:
        if u in seen:
            continue
        seen.add(u)
        f = uri_feedback_score(u, fb)
        t = uri_trust_score(u)
        r = uri_freshness_score(u)
        final = 0.50 * f + 0.30 * t + 0.20 * r
        scored.append((final, f, t, r, u))
    scored.sort(reverse=True, key=lambda x: x[0])
    return [x[4] for x in scored[:topn]], scored[:min(5, len(scored))]
