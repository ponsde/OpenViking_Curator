#!/usr/bin/env python3
import json, os, re, time
from pathlib import Path


def slug(s: str):
    return re.sub(r'[^a-zA-Z0-9_\-]+', '_', s).strip('_')[:80]


def capture_case(query: str, scope: dict, report: dict, answer: str, out_dir='cases'):
    p = Path(out_dir)
    p.mkdir(parents=True, exist_ok=True)
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    fn = p / f"{int(time.time())}_{slug(query)}.md"
    content = f"""# Case: {query}

- Date: {ts}
- Domain: {scope.get('domain','general')}
- Query: {query}
- Outcome: {'ok' if report.get('flags',{}).get('external_triggered') is not None else 'unknown'}
- Router confidence: {report.get('scores',{}).get('router_confidence')}
- Coverage before external: {report.get('scores',{}).get('coverage_before_external')}
- External triggered: {report.get('flags',{}).get('external_triggered')}
- Ingested: {report.get('flags',{}).get('ingested')}

## Reusable Steps
1. Scope routing
2. Local retrieval coverage gate
3. External fallback + review + optional ingest

## Final Answer (excerpt)
{answer[:1200]}
"""
    fn.write_text(content, encoding='utf-8')
    return str(fn)
