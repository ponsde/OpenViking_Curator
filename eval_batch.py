#!/usr/bin/env python3
"""Batch evaluation: 10 questions to verify no regression after modularization."""
import subprocess, time, json, sys, os

QUESTIONS = [
  # 库内（应高 coverage，不触发外搜）
  'OpenViking 的分层检索机制和优势是什么？',
  'grok2api 自动注册需要哪些前置配置和常见失败原因？',
  'newapi 对接 openai 兼容上游常见坑有哪些？',
  '如果要做 Curator 插件，v0.1 最小可行版本应该包含什么？',
  'OpenViking 和传统RAG差异是什么？',
  'Docker compose 和 systemd 管理服务各有什么优劣？',
  'Claude API 和 OpenAI API 兼容性差异有哪些？',
  'Nginx 反向代理 502 常见排查步骤？',
  # 库外（应低 coverage，触发外搜）
  'Deno 2.0 和 Bun 最新性能对比如何？2026年该选哪个？',
  'Rust 的 async runtime tokio vs async-std 怎么选？',
]

# Load simple .env (KEY=VALUE)
if os.path.exists('.env'):
    with open('.env', 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            os.environ[k.strip()] = v.strip().strip('"').strip("'")

rows = []
for q in QUESTIONS:
    t = time.time()
    p = subprocess.run(
        [sys.executable, 'curator_query.py', q],
        capture_output=True, text=True, timeout=600,
        env=os.environ.copy(),
    )
    elapsed = round(time.time() - t, 2)
    stdout = (p.stdout or '').strip()
    stderr = (p.stderr or '').strip()

    # Parse JSON output from curator_query.py
    ok = False
    routed = False
    answer_text = ''
    coverage = None
    external_triggered = False
    ingested = False
    error = ''

    try:
        result = json.loads(stdout)
        routed = result.get('routed', False)
        if routed:
            ok = bool(result.get('answer'))
            answer_text = (result.get('answer') or '')[:200]
            meta = result.get('meta', {})
            coverage = meta.get('coverage')
            external_triggered = meta.get('external_triggered', False)
            ingested = meta.get('ingested', False)
        else:
            ok = True  # Not routed is also a valid result
            answer_text = f"(not routed: {result.get('reason', '?')})"
        if result.get('error'):
            ok = False
            error = result['error']
    except json.JSONDecodeError:
        error = f"JSON parse error: {stdout[:100]}"

    # Also check stderr for STEP logs
    coverage_line = next((ln for ln in stderr.splitlines() if 'STEP 3' in ln and 'coverage' in ln), '')

    rows.append({
        'q': q,
        'sec': elapsed,
        'ok': ok,
        'routed': routed,
        'external': external_triggered,
        'ingested': ingested,
        'coverage': coverage,
        'coverage_log': coverage_line[:220] if coverage_line else '',
        'answer_preview': answer_text,
        'error': error,
    })

summary = {
    'count': len(rows),
    'ok': sum(r['ok'] for r in rows),
    'routed': sum(r['routed'] for r in rows),
    'external': sum(r['external'] for r in rows),
    'ingested': sum(r['ingested'] for r in rows),
    'avg_sec': round(sum(r['sec'] for r in rows) / len(rows), 2),
}

print(json.dumps({'summary': summary, 'rows': rows}, ensure_ascii=False, indent=2))
