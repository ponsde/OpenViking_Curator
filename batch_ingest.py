#!/usr/bin/env python3
"""
batch_ingest.py — 批量搜索并入库知识

用法:
  python3 batch_ingest.py                    # 运行所有预设话题
  python3 batch_ingest.py --topic "Docker常见问题"  # 运行单个话题
  python3 batch_ingest.py --dry              # 只搜索不入库
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

from curator_v0 import (
    validate_config, chat, route_scope, local_search,
    external_boost_needed, external_search, judge_and_pack,
    ingest_markdown, build_priority_context, detect_conflict,
    OAI_BASE, OAI_KEY, ANSWER_MODELS, OPENVIKING_CONFIG_FILE, DATA_PATH,
)
import openviking as ov

# ── 我们日常会遇到的话题 ──
TOPICS = [
    # 服务器运维
    "Linux VPS 安全加固最佳实践（SSH、防火墙、自动更新）",
    "Docker 容器常见问题排查（日志、网络、存储）",
    "Nginx 反向代理配置常见错误与排查方法",
    "systemd 服务管理：创建、调试、日志查看",

    # AI/LLM 工程
    "RAG 系统常见问题与优化策略（检索质量、chunk 大小、重排序）",
    "LLM API 网关对比：NewAPI vs OneAPI vs OpenRouter",
    "MCP (Model Context Protocol) 是什么？工作原理和使用场景",
    "向量数据库选型对比：Milvus vs Chroma vs Qdrant vs Weaviate",

    # 开发工具
    "Git 高级用法：rebase、cherry-pick、bisect、reflog",
    "Python asyncio 常见陷阱与最佳实践",
    "GitHub Actions CI/CD 入门与常见配置模式",

    # 我们项目相关
    "OpenViking 上下文文件系统的设计理念与使用方法",
    "Grok API 使用指南与常见限制",
    "Claude API vs OpenAI API 参数差异与兼容性注意事项",
]


def run_single(topic: str, client, dry=False) -> dict:
    """对单个话题执行: 检查本地→外搜→审核→入库"""
    result = {'topic': topic, 'status': 'skip', 'reason': ''}

    try:
        # 1. 路由
        scope = route_scope(topic)

        # 2. 本地检索
        local_txt, coverage, meta = local_search(client, topic, scope)

        # 3. 判断是否需要外搜
        boost, reason = external_boost_needed(topic, scope, coverage, meta)
        if not boost:
            result['status'] = 'skip'
            result['reason'] = f'local_sufficient (coverage={coverage:.2f})'
            return result

        # 4. 外搜
        print(f"  🔍 搜索中... (reason={reason})")
        ext_text = external_search(topic, scope)
        if not ext_text or len(ext_text) < 50:
            result['status'] = 'skip'
            result['reason'] = 'external_empty'
            return result

        # 5. 审核
        print(f"  🔎 审核中... ({len(ext_text)} 字)")
        judgment = judge_and_pack(topic, ext_text)
        if not judgment.get('pass'):
            result['status'] = 'rejected'
            result['reason'] = judgment.get('reason', 'quality_fail')
            return result

        # 6. 入库
        if dry:
            result['status'] = 'dry_pass'
            result['reason'] = f"trust={judgment.get('trust')}, would ingest"
            return result

        md = judgment.get('markdown', '')
        if md:
            ing = ingest_markdown(client, "curated", md)
            result['status'] = 'ingested'
            result['uri'] = ing.get('root_uri', '')
            result['trust'] = judgment.get('trust')
            print(f"  ✅ 入库: {result['uri']}")
        else:
            result['status'] = 'rejected'
            result['reason'] = 'no_markdown'

    except Exception as e:
        result['status'] = 'error'
        result['reason'] = str(e)[:200]

    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--topic', help='Single topic to ingest')
    parser.add_argument('--dry', action='store_true', help='Dry run')
    parser.add_argument('--delay', type=int, default=5, help='Delay between queries (seconds)')
    parser.add_argument('--retry', action='store_true', help='Only retry previously failed topics')
    parser.add_argument('--max-retries', type=int, default=2, help='Max retries per topic')
    args = parser.parse_args()

    validate_config()
    os.environ["OPENVIKING_CONFIG_FILE"] = OPENVIKING_CONFIG_FILE
    client = ov.SyncOpenViking(path=DATA_PATH)
    client.initialize()

    # 加载失败记录
    fail_log = Path(__file__).parent / '.failed_topics.json'
    prev_failed = []
    if fail_log.exists():
        try:
            prev_failed = json.loads(fail_log.read_text())
        except:
            pass

    if args.retry and prev_failed:
        topics = prev_failed
        print(f"🔄 重试模式: {len(topics)} 条之前失败的话题\n")
    elif args.topic:
        topics = [args.topic]
    else:
        topics = TOPICS

    results = []
    print(f"📚 批量入库: {len(topics)} 个话题 {'(DRY RUN)' if args.dry else ''}\n")

    try:
        for i, topic in enumerate(topics, 1):
            print(f"[{i}/{len(topics)}] {topic}")

            # 重试逻辑
            r = None
            for attempt in range(1, args.max_retries + 1):
                r = run_single(topic, client, dry=args.dry)
                if r['status'] in ('ingested', 'skip', 'dry_pass'):
                    break
                if attempt < args.max_retries:
                    wait = args.delay * attempt
                    print(f"  ⚠️ {r['status']}: {r.get('reason','')} — 等 {wait}s 重试 ({attempt}/{args.max_retries})")
                    time.sleep(wait)

            results.append(r)
            print(f"  → {r['status']}: {r.get('reason', r.get('uri', ''))}\n")

            if i < len(topics):
                time.sleep(args.delay)
    finally:
        try:
            client.close()
        except:
            pass

    # 记录失败话题供下次 --retry
    failed = [r['topic'] for r in results if r['status'] in ('error', 'rejected')]
    if failed:
        fail_log.write_text(json.dumps(failed, ensure_ascii=False, indent=2))
        print(f"\n💾 {len(failed)} 条失败已记录，下次用 --retry 重跑")
    elif fail_log.exists():
        fail_log.unlink()

    # 统计
    stats = {}
    for r in results:
        stats[r['status']] = stats.get(r['status'], 0) + 1

    print("=" * 50)
    print(f"📊 完成: {json.dumps(stats, ensure_ascii=False)}")
    ingested = [r for r in results if r['status'] == 'ingested']
    if ingested:
        print(f"✅ 新入库 {len(ingested)} 条:")
        for r in ingested:
            print(f"   {r['topic']} → {r.get('uri','')}")


if __name__ == '__main__':
    main()
