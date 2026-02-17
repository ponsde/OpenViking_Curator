#!/usr/bin/env python3
import subprocess, time, json, sys, os

QUESTIONS = [
  'OpenViking 的分层检索机制和优势是什么？',
  'grok2api 自动注册需要哪些前置配置和常见失败原因？',
  'newapi 对接 openai 兼容上游常见坑有哪些？',
  '如果要做 Curator 插件，v0.1 最小可行版本应该包含什么？',
  'OpenViking 和传统RAG差异是什么？'
]

# Load simple .env (KEY=VALUE)
if os.path.exists('.env'):
    with open('.env', 'r', encoding='utf-8') as f:
        for line in f:
            line=line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k,v=line.split('=',1)
            os.environ[k.strip()] = v.strip().strip('"').strip("'")

rows=[]
for q in QUESTIONS:
    t=time.time()
    p=subprocess.run([sys.executable,'curator_v0.py',q],capture_output=True,text=True,timeout=600, env=os.environ.copy())
    out=(p.stdout or '') + (p.stderr or '')
    rows.append({
      'q': q,
      'sec': round(time.time()-t,2),
      'ok': '===== FINAL ANSWER =====' in out,
      'external': '触发外部搜索' in out,
      'ingested': '✅ 已入库:' in out,
      'coverage': next((ln for ln in out.splitlines() if 'STEP 3 完成:' in ln), '')[:220]
    })

summary={
  'count': len(rows),
  'ok': sum(r['ok'] for r in rows),
  'external': sum(r['external'] for r in rows),
  'ingested': sum(r['ingested'] for r in rows),
  'avg_sec': round(sum(r['sec'] for r in rows)/len(rows),2)
}

print(json.dumps({'summary':summary,'rows':rows},ensure_ascii=False,indent=2))
