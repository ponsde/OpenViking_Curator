#!/usr/bin/env python3
"""
curator_query.py — 一体化查询入口
用法: python3 curator_query.py "你的问题"

输出 JSON:
  {"routed": false, "reason": "..."}                    — 不需要调插件
  {"routed": true, "answer": "...", "meta": {...}}      — 插件回答
"""
import json, os, re, sys
from pathlib import Path

# ── 快速门控（不调 LLM，纯规则） ──

# 明确需要插件的信号词
POSITIVE = [
    # 知识类
    r"是什么", r"怎么", r"原理", r"架构", r"区别", r"对比", r"比较",
    r"总结", r"文档", r"资料", r"教程", r"部署", r"配置",
    r"what\s+is", r"how\s+(does|do|to|can)", r"difference", r"compare",
    r"architecture", r"tutorial", r"deploy", r"setup",
    r"为什么", r"why", r"优缺点", r"pros?\s*(and|&)\s*cons?",
    r"best\s+practice", r"最佳实践",
    # 选型/推荐
    r"选", r"推荐", r"方案", r"选型", r"入门", r"指南", r"该用",
    r"recommend", r"which\s+(one|should)", r"suggest",
    # 经验召回
    r"历史", r"之前", r"上次", r"经验", r"参考",
    # 排查/调试
    r"排查", r"日志", r"故障", r"报错", r"错误", r"怎么看", r"怎么用",
    r"troubleshoot", r"error", r"log", r"debug", r"502|503|504|timeout",
    # 开发任务
    r"写一个", r"搭建", r"加固", r"注册", r"自动化",
    r"pipeline", r"优化", r"迁移", r"接入", r"对接",
    r"script", r"automat", r"build", r"implement", r"create",
    # 技术名词（出现就大概率是技术问题）
    r"docker|nginx|redis|mysql|postgres|k8s|kubernetes",
    r"api|sdk|cli|ssh|ssl|tls|http|websocket",
    r"python|node|rust|go|java|typescript",
    r"linux|ubuntu|centos|systemd",
    # 通用知识信号
    r"支持|兼容|版本|最新|功能|特性|渠道|安装|升级|更新",
    r"support|compatible|version|feature|install|upgrade|channel",
]

# 明确不需要的信号词（日常对话/操作指令）
# STRONG_NEGATIVE: 即使有正向信号也拦截（明确非技术场景）
STRONG_NEGATIVE = [
    r"天气|时间|几点|日期",
    r"提醒我|remind",
]
NEGATIVE = [
    r"^(hi|hello|hey|你好|嗨|早|晚安)\s*$",
    r"^(ok|好的|行|收到|嗯|谢谢|thanks)\s*$",
    r"帮我(跑|执行|运行|commit|push|重启)",
    r"(?<!\w)(git|cd|ls|cat|rm|mv)\s+",
    r"打开|关闭|启动|停止|重启",
]


def should_route(query: str) -> tuple[bool, str]:
    q = query.strip().lower()
    if len(q) < 4:
        return False, "too_short"

    # 强负向：即使有正向信号也拦截（明确非技术场景）
    for p in STRONG_NEGATIVE:
        if re.search(p, q, re.IGNORECASE):
            return False, "strong_negative"

    # 正向优先：有明确技术信号就路由
    has_positive = False
    for p in POSITIVE:
        if re.search(p, q, re.IGNORECASE):
            has_positive = True
            break

    if has_positive:
        return True, "positive_match"

    # 没有正向信号时，负向拦截
    for p in NEGATIVE:
        if re.search(p, q, re.IGNORECASE):
            return False, "negative_match"

    # 中长问句（>15字、含问号）倾向路由
    if len(q) > 15 and ('?' in q or '？' in q or '吗' in q or '呢' in q):
        return True, "question_heuristic"

    return False, "no_signal"


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
        import openviking as ov
        config_file = os.getenv("OPENVIKING_CONFIG_FILE", "")
        client = ov.SyncOpenViking(path=os.getenv("CURATOR_DATA_PATH", "./data"))
        client.initialize()
        resources = client.ls("viking://resources")
        result["openviking"] = {
            "status": "✅ connected",
            "config": config_file or "(default)",
            "resources": len(resources) if resources else 0,
        }
        client.close()
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


def run_curator(query: str) -> dict:
    """调用 curator.run() 获取结构化结果。"""
    load_env()
    sys.path.insert(0, str(Path(__file__).parent))

    try:
        from curator import run
        result = run(query)
    except Exception as e:
        return {"routed": True, "error": str(e)}

    return {
        "routed": True,
        "answer": result.get("answer", ""),
        "meta": {
            "duration": result.get("metrics", {}).get("duration_sec"),
            "external_triggered": result.get("meta", {}).get("external_triggered"),
            "external_reason": result.get("meta", {}).get("external_reason"),
            "has_conflict": result.get("meta", {}).get("has_conflict"),
            "ingested": result.get("meta", {}).get("ingested"),
            "coverage": result.get("meta", {}).get("coverage"),
            "case_path": result.get("case_path"),
        },
    }


HELP_TEXT = """OpenViking Curator — Knowledge-governed Q&A with retrieval + external search

Usage:
  python3 curator_query.py "your question"     Query with auto routing gate
  python3 curator_query.py --status             Health check (config + OpenViking + stats)
  python3 curator_query.py --help               Show this help

Environment:
  Configure via .env file (see .env.example) or env vars.
  Key settings: CURATOR_OAI_BASE, CURATOR_OAI_KEY, CURATOR_GROK_KEY

Output (JSON):
  {"routed": false, "reason": "..."}                Not routed (no plugin needed)
  {"routed": true, "answer": "...", "meta": {...}}   Plugin answer with metadata
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

    q = " ".join(args).strip()

    route, reason = should_route(q)
    if not route:
        print(json.dumps({"routed": False, "reason": reason}, ensure_ascii=False))
        sys.exit(0)

    result = run_curator(q)
    print(json.dumps(result, ensure_ascii=False, indent=2))
