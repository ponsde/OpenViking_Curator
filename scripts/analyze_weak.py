#!/usr/bin/env python3
"""弱点分析脚本：从 query_log.jsonl 识别知识弱点 topic。

用法:
    python scripts/analyze_weak.py [--min-queries N] [--data-dir PATH]
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

# Ensure curator package is importable when running as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from curator.nlp_utils import extract_keywords, extract_topic  # noqa: E402

# 默认 data 目录
DEFAULT_DATA_DIR = os.environ.get("CURATOR_DATA_PATH", str(Path(__file__).resolve().parent.parent / "data"))


def analyze(data_dir: str, min_queries: int = 2) -> list[dict]:
    """读取 query_log.jsonl，分析弱点 topic。"""
    log_path = os.path.join(data_dir, "query_log.jsonl")
    if not os.path.exists(log_path):
        print(f"[warn] query_log.jsonl 不存在: {log_path}", file=sys.stderr)
        return []

    # 读取所有日志
    entries = []
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not entries:
        return []

    # 按 topic 聚类
    topic_stats: dict[str, dict] = defaultdict(
        lambda: {
            "queries": [],
            "coverages": [],
            "external_count": 0,
            "total": 0,
        }
    )

    for entry in entries:
        topic = extract_topic(entry.get("query", ""))
        stats = topic_stats[topic]
        stats["queries"].append(entry.get("query", ""))
        stats["coverages"].append(entry.get("coverage", 0.0))
        if entry.get("external_triggered", False):
            stats["external_count"] += 1
        stats["total"] += 1

    # 计算统计 + 识别弱点
    all_topics = []
    weak_topics = []

    for topic, stats in topic_stats.items():
        count = stats["total"]
        avg_cov = sum(stats["coverages"]) / count if count else 0
        ext_rate = stats["external_count"] / count if count else 0

        item = {
            "topic": topic,
            "query_count": count,
            "avg_coverage": round(avg_cov, 4),
            "external_rate": round(ext_rate, 4),
        }
        all_topics.append(item)

        # 弱点判定：外搜触发率 > 50% 且查询次数 >= min_queries
        if ext_rate > 0.5 and count >= min_queries:
            weak_topics.append(item)

    # 按 external_rate 降序排列弱点
    weak_topics.sort(key=lambda x: (-x["external_rate"], -x["query_count"]))

    return weak_topics


def main():
    parser = argparse.ArgumentParser(description="分析 Curator query 日志，识别知识弱点")
    parser.add_argument("--min-queries", type=int, default=2, help="最小查询次数阈值（默认 2）")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="数据目录")
    args = parser.parse_args()

    weak = analyze(args.data_dir, args.min_queries)

    # 写入 weak_topics.json
    out_path = os.path.join(args.data_dir, "weak_topics.json")
    os.makedirs(args.data_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(weak, f, ensure_ascii=False, indent=2)

    # 输出报告
    print(f"共发现 {len(weak)} 个弱点 topic（min_queries={args.min_queries}）")
    for t in weak:
        print(
            f"  [{t['topic']}] queries={t['query_count']}, "
            f"avg_cov={t['avg_coverage']:.2f}, ext_rate={t['external_rate']:.0%}"
        )
    print(f"\n已写入: {out_path}")


if __name__ == "__main__":
    main()
