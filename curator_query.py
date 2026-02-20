#!/usr/bin/env python3
"""
curator_query.py — 一体化查询入口
用法: python3 curator_query.py "你的问题"

输出 JSON:
  {"routed": false, "reason": "..."}                    — 不需要调插件
  {"routed": true, "context_text": "...", "meta": {...}} — 插件结构化结果
"""
import json, os, re, sys
from pathlib import Path

# ── 路由门控：LLM 判断 + 规则兜底 ──

# 绝对拦截（无论如何不路由）
_HARD_BLOCK = [
    r"^(hi|hello|hey|你好|嗨|早|晚安|早安)\s*[!！.。]?$",
    r"^(ok|好的|行|收到|嗯|谢谢|thanks|thx|明白|了解|知道了)\s*[!！.。]?$",
    r"(天气|时间|几点|日期|提醒我|remind\s*me)",
]

# 绝对通过（明确技术查询，跳过 LLM 判断直接路由）
_HARD_PASS = [
    r"(docker|nginx|redis|mysql|postgres|k8s|kubernetes).*(部署|配置|排查|教程|对比|怎么)",
    r"(502|503|504|timeout|error|报错|故障).*(排查|怎么|日志|原因)",
    r"(grok2api|openviking|newapi|oneapi).*(配置|部署|注册|怎么|架构)",
]

_LLM_ROUTE_PROMPT = """你是一个路由判断器。判断用户的消息是否需要查询知识库来回答。

需要查知识库的场景：
- 知识类问题（原理、概念、区别、教程、怎么做）
- 需要参考之前的项目经验或开发记录
- 技术选型、对比、推荐
- 排查、故障、报错相关
- 涉及具体技术栈（Docker、Nginx、Python、API 等）
- 用户在问一个你可能不知道但知识库里可能有答案的问题

不需要查知识库的场景：
- 日常对话（你好、谢谢、聊天）
- 纯操作指令（帮我跑xxx、commit、重启、查看日志）
- 简单的是/否确认
- 上下文已经很明确的跟进讨论
- 天气、时间、提醒等非知识类请求

只回答一个 JSON: {"route": true/false, "reason": "一句话原因"}
不要输出其他内容。"""


def _llm_should_route(query: str) -> tuple[bool, str]:
    """用 LLM 判断是否需要路由到知识库。快速、低成本。"""
    import requests

    # 优先用 Grok（快+免费），fallback 到 OAI
    endpoints = []
    grok_base = os.getenv("CURATOR_GROK_BASE", "http://127.0.0.1:8000/v1")
    grok_key = os.getenv("CURATOR_GROK_KEY", "")
    if grok_key:
        endpoints.append((grok_base, grok_key, os.getenv("CURATOR_GROK_MODEL", "grok-4-fast")))
    
    oai_base = os.getenv("CURATOR_OAI_BASE", "")
    oai_key = os.getenv("CURATOR_OAI_KEY", "")
    if oai_key:
        endpoints.append((oai_base, oai_key, "gemini-3-flash-preview"))

    for base, key, model in endpoints:
        try:
            r = requests.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": _LLM_ROUTE_PROMPT},
                        {"role": "user", "content": query},
                    ],
                    "stream": False,
                    "max_tokens": 100,
                },
                timeout=15,
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            m = re.search(r'\{[^}]+\}', content)
            if m:
                parsed = json.loads(m.group(0))
                return bool(parsed.get("route", False)), parsed.get("reason", "llm_judge")
        except Exception:
            continue

    # LLM 全挂 → fallback 到规则
    return _rule_should_route(query)


def _rule_should_route(query: str) -> tuple[bool, str]:
    """纯规则 fallback（LLM 不可用时）。"""
    q = query.strip().lower()

    # 技术关键词
    tech_signals = [
        r"是什么|怎么|原理|架构|区别|对比|比较|部署|配置",
        r"what\s+is|how\s+(does|do|to|can)|difference|compare|deploy|setup",
        r"排查|日志|故障|报错|错误|troubleshoot|error|debug",
        r"docker|nginx|redis|api|sdk|ssh|python|linux|systemd",
        r"之前|经验|参考|上次|历史",
        r"选型|推荐|方案|recommend|suggest",
    ]
    for p in tech_signals:
        if re.search(p, q, re.IGNORECASE):
            return True, "rule_positive"

    if len(q) > 15 and ('?' in q or '？' in q or '吗' in q or '呢' in q):
        return True, "rule_question_heuristic"

    return False, "rule_no_signal"


def should_route(query: str) -> tuple[bool, str]:
    load_env()  # ensure .env is loaded for LLM routing
    q = query.strip()
    if len(q) < 4:
        return False, "too_short"

    ql = q.lower()

    # 硬拦截
    for p in _HARD_BLOCK:
        if re.search(p, ql, re.IGNORECASE):
            return False, "hard_block"

    # 硬通过
    for p in _HARD_PASS:
        if re.search(p, ql, re.IGNORECASE):
            return True, "hard_pass"

    # LLM 判断
    use_llm = os.getenv("CURATOR_LLM_ROUTE", "1") == "1"
    if use_llm:
        return _llm_should_route(q)
    else:
        return _rule_should_route(q)


def load_env():
    """Load .env file into os.environ."""
    env_file = Path(__file__).parent / '.env'
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())


def run_status() -> dict:
    """Quick health check: config, OpenViking connection, knowledge base stats."""
    load_env()
    sys.path.insert(0, str(Path(__file__).parent))

    result = {"config": {}, "openviking": {}, "local_index": {}, "feedback": {}, "cases": 0}

    # Config check
    for key in ["CURATOR_OAI_BASE", "CURATOR_OAI_KEY", "CURATOR_GROK_KEY"]:
        val = os.getenv(key, "")
        result["config"][key] = "✅ set" if val else "❌ missing"

    result["config"]["CURATOR_SEARCH_PROVIDER"] = os.getenv("CURATOR_SEARCH_PROVIDER", "grok (default)")

    # OpenViking check
    try:
        from curator.session_manager import OVClient
        ov = OVClient()
        healthy = ov.health()
        if healthy:
            resources = ov.ls("viking://resources")
            result["openviking"] = {
                "status": "✅ connected",
                "mode": ov.mode,
                "resources": len(resources) if resources else 0,
            }
        else:
            result["openviking"] = {"status": "❌ not healthy", "mode": ov.mode}
    except Exception as e:
        result["openviking"] = {"status": f"❌ {e}"}

    # Local index check
    idx_file = Path(__file__).parent / ".curated_index.json"
    if idx_file.exists():
        try:
            idx = json.loads(idx_file.read_text())
            result["local_index"] = {"documents": len(idx)}
        except Exception:
            result["local_index"] = {"status": "⚠️ corrupt"}
    else:
        result["local_index"] = {"status": "not found (run a query to generate)"}

    # Feedback check
    fb_file = Path(os.getenv("CURATOR_FEEDBACK_FILE", "./feedback.json"))
    if fb_file.exists():
        try:
            fb = json.loads(fb_file.read_text())
            result["feedback"] = {"entries": len(fb)}
        except Exception:
            result["feedback"] = {"entries": 0}
    else:
        result["feedback"] = {"entries": 0}

    # Cases count
    case_dir = Path(os.getenv("CURATOR_CASE_DIR", "./cases"))
    if case_dir.exists():
        result["cases"] = len(list(case_dir.glob("*.md")))
    else:
        result["cases"] = 0

    return result


def run_curator(query: str, auto_ingest: bool = True) -> dict:
    """调用 curator v2 pipeline 获取结构化结果。

    v2 不再生成回答，只返回结构化数据。调用方自己组装 LLM 上下文。
    auto_ingest=False 时进入审核模式：外搜结果不自动入库。
    """
    load_env()
    sys.path.insert(0, str(Path(__file__).parent))

    try:
        from curator.pipeline_v2 import run
        result = run(query, auto_ingest=auto_ingest)
    except Exception as e:
        return {"routed": True, "error": str(e)}

    # 组合上下文：本地 + 外搜，AI 直接拿去用
    context = result.get("context_text", "")
    external = result.get("external_text", "")
    if external:
        combined = f"{context}\n\n--- 以下为外部搜索补充 ---\n\n{external}"
    else:
        combined = context

    return {
        "routed": True,
        "query": result.get("query", query),
        "context": combined,  # AI 直接用这个
        "context_text": result.get("context_text", ""),
        "external_text": external,
        "coverage": result.get("coverage", 0),
        "conflict": result.get("conflict", {}),
        "meta": {
            "duration": result.get("metrics", {}).get("duration_sec"),
            "external_triggered": result.get("meta", {}).get("external_triggered"),
            "external_reason": result.get("meta", {}).get("external_reason"),
            "has_conflict": result.get("meta", {}).get("has_conflict"),
            "ingested": result.get("meta", {}).get("ingested"),
            "coverage": result.get("meta", {}).get("coverage"),
            "used_uris": result.get("meta", {}).get("used_uris", []),
            "case_path": result.get("case_path"),
        },
    }


HELP_TEXT = """OpenViking Curator — Knowledge-governed Q&A with retrieval + external search

Usage:
  python3 curator_query.py "your question"     Query with auto routing + auto ingest
  python3 curator_query.py --review "question"  Query but don't auto-ingest (human review mode)
  python3 curator_query.py --status             Health check (config + OpenViking + stats)
  python3 curator_query.py --help               Show this help

Environment:
  Configure via .env file (see .env.example) or env vars.
  Key settings: CURATOR_OAI_BASE, CURATOR_OAI_KEY, CURATOR_GROK_KEY

  CURATOR_CONFLICT_STRATEGY=auto|local|external|human
    auto (default): decide based on trust score + freshness
    local: always prefer local knowledge
    external: always prefer external source
    human: always flag for human review

Output (JSON):
  {"routed": false, "reason": "..."}                       Not routed (no plugin needed)
  {"routed": true, "context": "...", "meta": {...}}        Plugin structured result
"""


if __name__ == "__main__":
    args = sys.argv[1:]

    if not args or "--help" in args or "-h" in args:
        print(HELP_TEXT)
        sys.exit(0)

    if "--status" in args:
        result = run_status()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0)

    review_mode = "--review" in args or "--no-ingest" in args
    q = " ".join(a for a in args if not a.startswith("--")).strip()
    route, reason = should_route(q)
    if not route:
        print(json.dumps({"routed": False, "reason": reason}, ensure_ascii=False))
        sys.exit(0)

    result = run_curator(q, auto_ingest=not review_mode)
    if review_mode:
        result["review_mode"] = True
    print(json.dumps(result, ensure_ascii=False, indent=2))
