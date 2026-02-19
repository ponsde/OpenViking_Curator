#!/usr/bin/env python3
"""
Curator Eval Benchmark — 对比裸 OpenViking 检索 vs Curator 全流程。

公平对比：两边都比检索内容的关键词命中率。
- raw OV：检索结果的 L2 content
- Curator：检索结果的 context_text（不是 LLM 生成的 answer）

用法:
  cd /home/ponsde/OpenViking_test && source .venv/bin/activate
  python3 /home/ponsde/OpenViking_Curator/eval/benchmark.py

输出: eval/results/benchmark_YYYY-MM-DD.json + 终端表格
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Setup ──
sys.path.insert(0, '/home/ponsde/OpenViking_Curator')

# 加载 .env
from pathlib import Path as _Path
_env_file = _Path('/home/ponsde/OpenViking_Curator/.env')
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip())

os.environ.setdefault('OPENVIKING_CONFIG_FILE', '/home/ponsde/OpenViking_test/ov.conf')
os.environ.setdefault('CURATOR_DATA_PATH', '/home/ponsde/OpenViking_test/data')

# ── 10 个固定测试 Query ──
BENCHMARK_QUERIES = [
    {
        "id": 1,
        "query": "grok2api 怎么部署和配置",
        "expected_topics": ["grok2api", "端口", "8000", "API", "代理"],
        "category": "部署经验",
    },
    {
        "id": 2,
        "query": "OpenViking search 和 find 结果不一致怎么办",
        "expected_topics": ["非确定性", "Jaccard", "向量检索", "本地索引", "缓解"],
        "category": "踩坑经验",
    },
    {
        "id": 3,
        "query": "Curator 路由怎么从正则升级到 LLM",
        "expected_topics": ["LLM", "意图", "硬拦截", "Grok", "规则"],
        "category": "架构决策",
    },
    {
        "id": 4,
        "query": "SSE 流式响应导致 JSON 解析失败",
        "expected_topics": ["SSE", "stream", "false", "grok2api", "JSON"],
        "category": "Bug 修复",
    },
    {
        "id": 5,
        "query": "Telegram 里消息只能看到最后一段",
        "expected_topics": ["streamMode", "off", "partial", "Telegram"],
        "category": "配置经验",
    },
    {
        "id": 6,
        "query": "Python import 缺失被 try except 吞掉怎么排查",
        "expected_topics": ["import", "try", "except", "静默失败", "隐式依赖"],
        "category": "调试经验",
    },
    {
        "id": 7,
        "query": "怎么把文件存入 OpenViking 知识库",
        "expected_topics": ["add_resource", "target", "curated", "wait", "URI"],
        "category": "使用指南",
    },
    {
        "id": 8,
        "query": "curator 单文件重构为模块包的经验",
        "expected_topics": ["模块化", "config", "router", "pipeline", "重构"],
        "category": "架构经验",
    },
    {
        "id": 9,
        "query": "怎么做交叉验证防止回答不准确",
        "expected_topics": ["交叉验证", "cross_validate", "外搜", "入库", "冲突"],
        "category": "策略经验",
    },
    {
        "id": 10,
        "query": "URI 新鲜度和信任度怎么打分",
        "expected_topics": ["freshness", "trust", "时间衰减", "feedback", "score"],
        "category": "算法实现",
    },
]


def run_raw_ov(query: str, limit: int = 5) -> dict:
    """裸 OpenViking 检索（HTTP API，不经过 Curator）。返回 L2 content。"""
    import urllib.request

    def _post(path: str, payload: dict):
        req = urllib.request.Request(
            f"http://127.0.0.1:9100{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())

    def _get(path: str):
        with urllib.request.urlopen(f"http://127.0.0.1:9100{path}", timeout=60) as resp:
            return json.loads(resp.read())

    start = time.time()
    results = []
    seen = set()

    for path in ["/api/v1/search/search", "/api/v1/search/find"]:
        try:
            res = _post(path, {"query": query, "limit": limit}).get("result", {})
            for x in (res.get("resources", []) or []):
                u = x.get("uri", "")
                if u and u not in seen:
                    seen.add(u)
                    try:
                        import urllib.parse
                        enc = urllib.parse.quote(u, safe='/:')
                        content = (_get(f"/api/v1/content/read?uri={enc}").get("result", "") or "")[:1000]
                    except Exception:
                        content = x.get("abstract", "") or ""
                    results.append({"uri": u, "content": content})
        except Exception:
            pass

    elapsed = time.time() - start
    return {"results": results[:limit], "elapsed": round(elapsed, 2)}


def run_curator(query: str) -> dict:
    """Curator v2 全流程。返回 context_text（检索内容，非 LLM 回答）。"""
    import signal

    class TimeoutError(Exception):
        pass

    def _handler(signum, frame):
        raise TimeoutError("curator timeout")

    start = time.time()
    try:
        old_handler = signal.signal(signal.SIGALRM, _handler)
        signal.alarm(120)
        from curator.pipeline_v2 import run
        result = run(query)
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        elapsed = time.time() - start
        return {
            "context_text": result.get("context_text", ""),
            "external_text": result.get("external_text", ""),
            "coverage": result.get("coverage", 0),
            "routed": True,
            "elapsed": round(elapsed, 2),
        }
    except Exception as e:
        signal.alarm(0)
        elapsed = time.time() - start
        return {"context_text": "", "error": str(e), "elapsed": round(elapsed, 2)}


def score_hit(content: str, expected_topics: list) -> dict:
    """计算期望关键词的命中率"""
    content_lower = content.lower()
    hits = []
    misses = []
    for topic in expected_topics:
        if topic.lower() in content_lower:
            hits.append(topic)
        else:
            misses.append(topic)
    return {
        "hit_rate": len(hits) / max(1, len(expected_topics)),
        "hits": hits,
        "misses": misses,
    }


def run_benchmark():
    """运行完整 benchmark"""
    results = []

    for q in BENCHMARK_QUERIES:
        print(f"\n{'='*60}")
        print(f"[{q['id']}/10] {q['category']}: {q['query']}")
        print(f"{'='*60}")

        # 1. 裸 OV：用 L2 content
        raw = run_raw_ov(q["query"])
        raw_content = "\n".join(r["content"] for r in raw["results"])
        raw_score = score_hit(raw_content, q["expected_topics"])

        # 2. Curator v2：用 context_text（检索内容，不是 LLM 生成的 answer）
        cur = run_curator(q["query"])
        cur_content = cur.get("context_text", "") + " " + cur.get("external_text", "")
        cur_score = score_hit(cur_content, q["expected_topics"])

        entry = {
            "id": q["id"],
            "query": q["query"],
            "category": q["category"],
            "raw_ov": {
                "hit_rate": raw_score["hit_rate"],
                "hits": raw_score["hits"],
                "misses": raw_score["misses"],
                "n_results": len(raw["results"]),
                "elapsed": raw["elapsed"],
            },
            "curator": {
                "hit_rate": cur_score["hit_rate"],
                "hits": cur_score["hits"],
                "misses": cur_score["misses"],
                "routed": cur.get("routed", False),
                "coverage": cur.get("coverage", 0),
                "elapsed": cur.get("elapsed", 0),
                "error": cur.get("error", ""),
            },
            "winner": "curator" if cur_score["hit_rate"] > raw_score["hit_rate"]
                      else "raw" if raw_score["hit_rate"] > cur_score["hit_rate"]
                      else "tie",
        }
        results.append(entry)

        # 实时输出
        print(f"  裸 OV:    命中 {raw_score['hit_rate']:.0%} ({len(raw_score['hits'])}/{len(q['expected_topics'])})  {raw['elapsed']:.1f}s")
        print(f"  Curator:  命中 {cur_score['hit_rate']:.0%} ({len(cur_score['hits'])}/{len(q['expected_topics'])})  {cur.get('elapsed', 0):.1f}s")
        print(f"  胜者: {entry['winner']}")

    # ── 汇总 ──
    print(f"\n{'='*60}")
    print("📊 汇总")
    print(f"{'='*60}")

    raw_avg = sum(r["raw_ov"]["hit_rate"] for r in results) / len(results)
    cur_avg = sum(r["curator"]["hit_rate"] for r in results) / len(results)
    raw_time = sum(r["raw_ov"]["elapsed"] for r in results) / len(results)
    cur_time = sum(r["curator"]["elapsed"] for r in results) / len(results)

    wins = {"curator": 0, "raw": 0, "tie": 0}
    for r in results:
        wins[r["winner"]] += 1

    print(f"  裸 OV 平均命中率:   {raw_avg:.0%}  平均耗时: {raw_time:.1f}s")
    print(f"  Curator 平均命中率: {cur_avg:.0%}  平均耗时: {cur_time:.1f}s")
    print(f"  提升: {(cur_avg - raw_avg) / max(0.01, raw_avg) * 100:+.0f}%")
    print(f"  胜负: Curator {wins['curator']} / 裸OV {wins['raw']} / 平 {wins['tie']}")

    # ── 保存结果 ──
    out_dir = Path("/home/ponsde/OpenViking_Curator/eval/results")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"benchmark_{datetime.now().strftime('%Y-%m-%d_%H%M')}.json"

    summary = {
        "timestamp": datetime.now().isoformat(),
        "n_queries": len(results),
        "raw_avg_hit_rate": round(raw_avg, 3),
        "curator_avg_hit_rate": round(cur_avg, 3),
        "improvement_pct": round((cur_avg - raw_avg) / max(0.01, raw_avg) * 100, 1),
        "wins": wins,
        "raw_avg_time": round(raw_time, 2),
        "curator_avg_time": round(cur_time, 2),
        "details": results,
    }
    out_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n📁 结果已保存: {out_file}")

    return summary


if __name__ == "__main__":
    run_benchmark()
