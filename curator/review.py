"""Review: judge external results, ingest, conflict detection.

B2 优化：judge_and_ingest 合并审核+冲突检测为一次 LLM 调用。
"""

import json
import os
import re
import time
import datetime
from pathlib import Path


def _extract_json(text: str) -> str | None:
    """从文本中提取第一个完整的 JSON 对象（括号深度匹配）。

    比 re.search(r"\\{[\\s\\S]*\\}") 更安全：
    - 贪婪 regex 遇到嵌套 JSON 或多个 JSON 块会匹配过头
    - 这里用括号计数，只返回第一个平衡的 {...}
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False

    for i in range(start, len(text)):
        ch = text[i]

        if escape:
            escape = False
            continue

        if ch == "\\":
            if in_string:
                escape = True
            continue

        if ch == '"' and not escape:
            in_string = not in_string
            continue

        if in_string:
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]

    return None

from .config import (
    log, chat,
    OAI_BASE, OAI_KEY, JUDGE_MODEL, JUDGE_MODELS, CURATED_DIR,
)


def judge_and_ingest(ov_client, query: str, local_ctx: str, external_text: str) -> dict:
    """B2: 合并审核 + 冲突检测为一次 LLM 调用。

    返回:
        {
            "pass": bool,          # 是否值得入库
            "reason": str,
            "trust": int,          # 0-10
            "freshness": str,      # current/recent/outdated/unknown
            "markdown": str,       # 审核通过后的入库内容
            "has_conflict": bool,  # 本地 vs 外部是否有冲突
            "conflict_summary": str,
            "conflict_points": list,
        }
    """
    today = datetime.date.today().isoformat()

    # 截断上下文，控制 prompt 长度
    local_snippet = (local_ctx or "")[:2000]
    external_snippet = (external_text or "")[:3000]

    sys_prompt = (
        "你是知识库治理助手。你需要同时完成两件事：\n\n"
        "1. **审核外搜结果**：判断是否值得入库\n"
        "   - 内容准确性、时效性、入库价值\n"
        "   - 超过1年未更新的标注[可能过时]\n"
        "   - 已取消/变更的功能当作当前事实 → pass=false\n\n"
        "2. **冲突检测**：比较本地知识与外搜结果是否有结论冲突\n"
        "   - 细节差异不算冲突，结论矛盾才算\n\n"
        f"当前日期: {today}\n\n"
        "输出严格 JSON:\n"
        "{\n"
        '  "pass": bool,\n'
        '  "reason": "审核判断理由",\n'
        '  "trust": 0-10,\n'
        '  "freshness": "current|recent|outdated|unknown",\n'
        '  "summary": "内容摘要",\n'
        '  "markdown": "如果 pass=true，输出整理后的 markdown（含来源URL和日期）",\n'
        '  "has_conflict": bool,\n'
        '  "conflict_summary": "冲突摘要（无冲突则空）",\n'
        '  "conflict_points": ["冲突点1", "冲突点2"]\n'
        "}\n只输出 JSON。"
    )

    user_content = (
        f"用户问题: {query}\n\n"
        f"本地知识:\n{local_snippet}\n\n"
        f"外搜结果:\n{external_snippet}"
    )

    last_err = None
    out = None
    for jm in JUDGE_MODELS:
        try:
            out = chat(OAI_BASE, OAI_KEY, jm, [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_content},
            ], timeout=90)
            break
        except Exception as e:
            last_err = e
            continue

    default = {
        "pass": False, "reason": f"judge_fail:{last_err}",
        "trust": 0, "freshness": "unknown", "summary": "", "markdown": "",
        "has_conflict": False, "conflict_summary": "", "conflict_points": [],
    }

    if out is None:
        return default

    json_str = _extract_json(out)
    if not json_str:
        default["reason"] = "bad_json"
        return default

    try:
        result = json.loads(json_str)
        # 确保所有字段存在
        result.setdefault("pass", False)
        result.setdefault("reason", "")
        result.setdefault("trust", 0)
        result.setdefault("freshness", "unknown")
        result.setdefault("summary", "")
        result.setdefault("markdown", "")
        result.setdefault("has_conflict", False)
        result.setdefault("conflict_summary", "")
        result.setdefault("conflict_points", [])
        if not isinstance(result["conflict_points"], list):
            result["conflict_points"] = []
        return result
    except Exception:
        default["reason"] = "json_parse_fail"
        return default


def judge_and_pack(query: str, external_text: str):
    """Legacy: 单独审核（不含冲突检测）。仅供测试/兼容。"""
    today = datetime.date.today().isoformat()
    sys = (
        "你是资料审核器。判断外部搜索结果是否值得入库。\n"
        f"当前日期: {today}\n\n"
        "输出严格JSON: pass(bool), reason(string), tags(array), trust(0-10), "
        "freshness(string: current/recent/outdated/unknown), "
        "summary(string), markdown(string)。只输出JSON。"
    )

    last_err = None
    out = None
    for jm in JUDGE_MODELS:
        try:
            out = chat(OAI_BASE, OAI_KEY, jm, [
                {"role": "system", "content": sys},
                {"role": "user", "content": f"用户问题:{query}\n候选资料:\n{external_text}"},
            ], timeout=90)
            break
        except Exception as e:
            last_err = e
            continue

    if out is None:
        return {"pass": False, "reason": f"judge_model_fail:{last_err}", "tags": [], "trust": 0, "summary": "", "markdown": ""}

    json_str = _extract_json(out)
    if not json_str:
        return {"pass": False, "reason": "bad_json", "tags": [], "trust": 0, "summary": "", "markdown": ""}
    try:
        return json.loads(json_str)
    except Exception:
        return {"pass": False, "reason": "json_parse_fail", "tags": [], "trust": 0, "summary": "", "markdown": ""}


def detect_conflict(query: str, local_ctx: str, external_ctx: str):
    """Legacy: 单独冲突检测。仅供测试/兼容。"""
    if not external_ctx.strip():
        return {"has_conflict": False, "summary": "", "points": []}

    sys = (
        "你是冲突检测器。比较本地上下文与外部补充是否存在结论冲突。"
        "输出严格JSON：has_conflict(bool), summary(string), points(array of string)。"
        "如果只是细节差异但不影响结论，has_conflict=false。只输出JSON。"
    )
    out = chat(OAI_BASE, OAI_KEY, JUDGE_MODEL, [
        {"role": "system", "content": sys},
        {"role": "user", "content": f"问题:{query}\n\n本地:\n{local_ctx[:2500]}\n\n外部:\n{external_ctx[:2500]}"},
    ], timeout=60)
    json_str = _extract_json(out)
    if not json_str:
        return {"has_conflict": False, "summary": "", "points": []}
    try:
        j = json.loads(json_str)
        if "points" not in j or not isinstance(j.get("points"), list):
            j["points"] = []
        return j
    except Exception:
        return {"has_conflict": False, "summary": "", "points": []}


def ingest_markdown_v2(ov_client, title: str, markdown: str, freshness: str = "unknown"):
    """入库 markdown 到 OV。

    嵌入模式：写本地文件 → add_resource(path)
    HTTP 模式：先写本地备份，再用 OVClient 公开方法入库。
               如果 add_resource 失败（远端读不到本地路径），会 warning。
    """
    today = datetime.date.today().isoformat()
    ttl_map = {"current": 180, "recent": 90, "unknown": 60, "outdated": 0}
    ttl_days = ttl_map.get(freshness, 60)

    header = (
        f"<!-- curator_meta: ingested={today} freshness={freshness} ttl_days={ttl_days} -->\n"
        f"<!-- review_after: {(datetime.date.today() + datetime.timedelta(days=ttl_days)).isoformat()} -->\n\n"
    )

    full_content = header + markdown

    # 写本地文件（嵌入模式用于入库，HTTP 模式做备份）
    p = Path(CURATED_DIR)
    p.mkdir(parents=True, exist_ok=True)
    fn = p / f"{int(time.time())}_{re.sub(r'[^a-zA-Z0-9_-]+', '_', title)[:40]}.md"
    fn.write_text(full_content, encoding="utf-8")

    # 检测 OV 模式
    is_http = bool(os.environ.get("OV_BASE_URL", "").strip()) or getattr(ov_client, 'mode', '') == 'http'

    if is_http:
        log.info("ingest_markdown_v2: HTTP 模式，本地备份已写 %s", fn)
        # HTTP 模式下 add_resource(local_path) 可能远端不可达
        # OV HTTP API 的 add_resource 某些部署支持 URL/内容上传，某些不支持
        # 这里仍然调用，让 OV 自己决定能不能处理
        try:
            result = ov_client.add_resource(str(fn), reason="curator_ingest")
            log.info("HTTP 模式 add_resource 返回: %s", result)
            return result
        except Exception as e:
            log.warning(
                "HTTP 模式 add_resource 失败（远端可能读不到本地路径 %s）: %s。"
                "内容已备份到本地文件，可手动入库。", fn, e
            )
            return {"status": "local_backup_only", "path": str(fn), "error": str(e)}
    else:
        # 嵌入模式：add_resource + wait_processed
        result = ov_client.add_resource(str(fn), reason="curator_ingest")
        try:
            ov_client.wait_processed(timeout=30)
        except Exception as e:
            log.debug("wait_processed 失败: %s", e)
        return result
