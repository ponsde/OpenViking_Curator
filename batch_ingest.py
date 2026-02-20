#!/usr/bin/env python3
"""
batch_ingest.py — 批量搜索并入库知识（v2, 适配 pipeline_v2）

用法:
  python3 batch_ingest.py                    # 运行所有预设话题
  python3 batch_ingest.py --topic "Docker常见问题"  # 运行单个话题
  python3 batch_ingest.py --dry              # 只搜索不入库（打印结果）
"""
import os, sys, json, time, argparse
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 加载 .env
env_file = Path(__file__).parent / '.env'
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            k, v = k.strip(), v.strip()
            if v:
                os.environ[k] = v

from curator import validate_config, run, OAI_BASE, OAI_KEY

TOPICS = [
    "Linux VPS 安全加固最佳实践（SSH、防火墙、自动更新）",
    "Docker 容器常见问题排查（日志、网络、存储）",
    "Nginx 反向代理配置常见错误与排查方法",
    "systemd 服务管理：创建、调试、日志查看",
    "OpenViking 架构与核心概念",
    "grok2api 部署与配置指南",
    "MCP (Model Context Protocol) 协议概述与使用场景",
    "RAG 检索增强生成：常见陷阱与优化方向",
    "Python asyncio 常见错误与 debug 技巧",
    "AI Agent 框架对比：LangChain vs LlamaIndex vs OpenClaw",
]


def main():
    parser = argparse.ArgumentParser(description="Batch ingest knowledge via Curator v2")
    parser.add_argument("--topic", help="运行单个话题")
    parser.add_argument("--dry", action="store_true", help="只搜索不入库")
    args = parser.parse_args()

    validate_config()
    topics = [args.topic] if args.topic else TOPICS

    results = []
    for i, topic in enumerate(topics):
        print(f"\n[{i+1}/{len(topics)}] {topic}")
        try:
            r = run(topic)
            ingested = r.get("meta", {}).get("ingested", False)
            coverage = r.get("coverage", 0)
            external = r.get("meta", {}).get("external_triggered", False)
            print(f"  coverage={coverage:.2f}, external={external}, ingested={ingested}")
            results.append({"topic": topic, "coverage": coverage, "external": external, "ingested": ingested})
        except Exception as e:
            print(f"  ERROR: {e}")
            results.append({"topic": topic, "error": str(e)})
        time.sleep(1)

    print(f"\n=== 完成: {len(results)} 个话题 ===")
    ingested_count = sum(1 for r in results if r.get("ingested"))
    print(f"入库: {ingested_count}, 跳过: {len(results) - ingested_count}")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
