#!/usr/bin/env python3
"""主动补强脚本：对弱 topic 触发外搜入库，提升覆盖率。

用法:
    python scripts/strengthen.py [--top N] [--dry] [--data-dir PATH]
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_DATA_DIR = os.environ.get("CURATOR_DATA_PATH", str(Path(__file__).resolve().parent.parent / "data"))


def load_env():
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def strengthen(data_dir: str, top_n: int = 3, dry: bool = False) -> list[dict]:
    """读取 weak_topics.json，对 top N 弱 topic 跑 pipeline 补强。"""
    weak_path = os.path.join(data_dir, "weak_topics.json")
    if not os.path.exists(weak_path):
        print(f"[warn] weak_topics.json 不存在: {weak_path}", file=sys.stderr)
        print("请先运行 scripts/analyze_weak.py", file=sys.stderr)
        return []

    with open(weak_path, "r", encoding="utf-8") as f:
        weak_topics = json.load(f)

    if not weak_topics:
        print("没有弱点 topic 需要补强。")
        return []

    targets = weak_topics[:top_n]
    print(f"将对 {len(targets)} 个弱 topic 进行补强：")
    for t in targets:
        print(f"  [{t['topic']}] avg_cov={t['avg_coverage']:.2f}, ext_rate={t['external_rate']:.0%}")

    if dry:
        print("\n--dry 模式，不实际执行。")
        return [{"topic": t["topic"], "status": "dry_run"} for t in targets]

    # 实际执行
    load_env()
    from curator.pipeline_v2 import run

    results = []
    for i, t in enumerate(targets):
        topic = t["topic"]
        query = f"{topic} 最佳实践与常见问题"
        print(f"\n[{i+1}/{len(targets)}] 补强: {topic}")
        print(f"  query: {query}")

        try:
            r = run(query)
            coverage = r.get("coverage", 0)
            external = r.get("meta", {}).get("external_triggered", False)
            ingested = r.get("meta", {}).get("ingested", False)
            print(f"  结果: coverage={coverage:.2f}, external={external}, ingested={ingested}")
            results.append({
                "topic": topic,
                "query": query,
                "coverage": coverage,
                "external_triggered": external,
                "ingested": ingested,
                "status": "ok",
            })
        except Exception as e:
            print(f"  ERROR: {e}")
            results.append({"topic": topic, "query": query, "status": "error", "error": str(e)})

        time.sleep(1)

    # 写补强报告
    report_path = os.path.join(data_dir, "strengthen_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    # 总结
    ok = sum(1 for r in results if r["status"] == "ok")
    ingested = sum(1 for r in results if r.get("ingested"))
    print(f"\n=== 补强完成: {ok}/{len(results)} 成功, {ingested} 条入库 ===")
    print(f"报告: {report_path}")
    return results


def main():
    parser = argparse.ArgumentParser(description="对弱 topic 主动补强")
    parser.add_argument("--top", type=int, default=3, help="补强 top N 个弱 topic（默认 3）")
    parser.add_argument("--dry", action="store_true", help="只打印，不实际执行")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="数据目录")
    args = parser.parse_args()

    strengthen(args.data_dir, args.top, args.dry)


if __name__ == "__main__":
    main()
