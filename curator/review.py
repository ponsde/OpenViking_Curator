"""Review: judge external results, ingest, conflict detection.

B2 优化：judge_and_ingest 合并审核+冲突检测为一次 LLM 调用。

All knowledge-store operations go through KnowledgeBackend; no direct OVClient use.
"""

from __future__ import annotations

import json
import os
import re
import time
import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Optional

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from .backend import KnowledgeBackend


# ── Pydantic model for judge output ──


class JudgeResult(BaseModel):
    """Structured output from the judge LLM call.

    Validates and normalises the JSON produced by the judge prompt.
    Uses ``alias="pass"`` because ``pass`` is a Python keyword.
    """

    passed: bool = Field(default=False, alias="pass")
    reason: str = ""
    trust: int = Field(default=0, ge=0, le=10)
    freshness: Literal["current", "recent", "outdated", "unknown"] = "unknown"
    summary: str = ""
    markdown: str = ""
    has_conflict: bool = False
    conflict_summary: str = ""
    conflict_points: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    def to_pipeline_dict(self) -> dict:
        """Convert to the dict format expected by pipeline_v2.

        Returns:
            Dict with ``"pass"`` key (not ``"passed"``), compatible with
            the existing pipeline result structure.
        """
        return {
            "pass": self.passed,
            "reason": self.reason,
            "trust": self.trust,
            "freshness": self.freshness,
            "summary": self.summary,
            "markdown": self.markdown,
            "has_conflict": self.has_conflict,
            "conflict_summary": self.conflict_summary,
            "conflict_points": list(self.conflict_points),
        }


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
    OAI_BASE, OAI_KEY, JUDGE_MODEL, JUDGE_MODELS, CURATED_DIR, CURATOR_VERSION,
    AUTO_SUMMARIZE, SUMMARIZE_MODELS,
)


def _parse_judge_output(raw_text: str | None, fallback_reason: str = "") -> JudgeResult:
    """Parse LLM output into a validated JudgeResult.

    Args:
        raw_text: Raw LLM response text (may contain JSON).
        fallback_reason: Reason string if parsing fails entirely.

    Returns:
        A validated :class:`JudgeResult`.
    """
    if raw_text is None:
        return JudgeResult(**{"pass": False, "reason": fallback_reason or "no_response"})

    json_str = _extract_json(raw_text)
    if not json_str:
        return JudgeResult(**{"pass": False, "reason": fallback_reason or "bad_json"})

    try:
        return JudgeResult.model_validate_json(json_str)
    except Exception:
        # Fallback: try plain json.loads then construct
        try:
            data = json.loads(json_str)
            # Ensure conflict_points is a list
            if not isinstance(data.get("conflict_points"), list):
                data["conflict_points"] = []
            return JudgeResult.model_validate(data)
        except Exception:
            return JudgeResult(**{"pass": False, "reason": fallback_reason or "json_parse_fail"})


def judge_and_ingest(backend: KnowledgeBackend, query: str,
                     local_ctx: str, external_text: str,
                     cv_warnings: list = ()) -> dict:
    """B2: 合并审核 + 冲突检测为一次 LLM 调用。

    Args:
        backend: Knowledge backend (used for type context only here;
                 actual ingest is done by caller via :func:`ingest_markdown_v2`).
        query: User query string.
        local_ctx: Local context text from OV.
        external_text: External search result text.
        cv_warnings: Optional list of risk warnings from cross_validate().
            Injected into sys_prompt so they never compete with the
            external_text[:3000] budget.

    Returns:
        Dict with keys: ``pass``, ``reason``, ``trust``, ``freshness``,
        ``markdown``, ``has_conflict``, ``conflict_summary``, ``conflict_points``.
    """
    today = datetime.date.today().isoformat()

    # 截断上下文，控制 prompt 长度
    local_snippet = (local_ctx or "")[:2000]
    external_snippet = (external_text or "")[:3000]

    # cv_warnings 注入 sys_prompt，不占 external_text 预算
    warnings_block = ""
    if cv_warnings:
        joined = "\n".join(cv_warnings)
        warnings_block = (
            f"\n\n⚠️ cross_validate 风险标注（审核时必须考虑）:\n{joined}"
        )

    sys_prompt = (
        "你是知识库治理助手。你需要同时完成两件事：\n\n"
        "1. **审核外搜结果**：判断是否值得入库\n"
        "   - 内容准确性、时效性、入库价值\n"
        "   - 超过1年未更新的标注[可能过时]\n"
        "   - 已取消/变更的功能当作当前事实 → pass=false\n\n"
        "2. **冲突检测**：比较本地知识与外搜结果是否有结论冲突\n"
        "   - 细节差异不算冲突，结论矛盾才算\n\n"
        f"当前日期: {today}"
        f"{warnings_block}\n\n"
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

    result = _parse_judge_output(out, fallback_reason=f"judge_fail:{last_err}")
    return result.to_pipeline_dict()


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


def _auto_summarize(content: str, title: str) -> dict:
    """生成 L0 abstract + L1 overview（一次 LLM call，可选功能）。

    Best-effort：失败时返回空 dict，不影响入库主流程。
    需要 AUTO_SUMMARIZE=1 且 OAI_BASE 配置正确。

    Args:
        content: 完整 markdown 正文（不含 curator_meta header）。
        title: 文档标题，用于 prompt 上下文。

    Returns:
        ``{"abstract": str, "overview": str}`` 或 ``{}``（失败时）。
    """
    if not OAI_BASE:
        return {}

    snippet = content[:4000]
    sys_prompt = (
        "你是文档摘要助手。根据给定的文档内容，输出严格 JSON（只输出 JSON）：\n"
        "{\n"
        '  "abstract": "80字以内的一句话摘要（L0，用于快速过滤）",\n'
        '  "overview": "关键要点列表，Markdown bullet 格式，不超过300字（L1，用于快速阅读）"\n'
        "}"
    )
    user_content = f"文档标题：{title}\n\n{snippet}"

    last_err: Exception | None = None
    for model in SUMMARIZE_MODELS:
        try:
            raw = chat(OAI_BASE, OAI_KEY, model, [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_content},
            ], timeout=30)
            # 从 raw 里提取 JSON
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if not m:
                continue
            data = json.loads(m.group())
            abstract = str(data.get("abstract", "")).strip()
            overview = str(data.get("overview", "")).strip()
            if abstract:
                return {"abstract": abstract, "overview": overview}
        except Exception as e:
            last_err = e
            log.debug("_auto_summarize model=%s error=%s", model, e)

    if last_err:
        log.debug("_auto_summarize skipped (all models failed): %s", last_err)
    return {}


def ingest_markdown_v2(backend: KnowledgeBackend, title: str,
                       markdown: str, freshness: str = "unknown",
                       source_urls: list[str] | None = None,
                       quality_feedback: dict | None = None):
    """入库 markdown 到知识后端。

    Builds a curator_meta header, writes local backup, then ingests
    via the backend's :meth:`ingest` method.

    Args:
        backend: Knowledge backend to ingest into.
        title: Document title.
        markdown: Markdown content.
        freshness: One of ``current``, ``recent``, ``outdated``, ``unknown``.

    Returns:
        Dict with at least ``root_uri`` key.
    """
    today = datetime.date.today().isoformat()
    ttl_map = {"current": 180, "recent": 90, "unknown": 60, "outdated": 0}
    ttl_days = ttl_map.get(freshness, 60)

    # L0/L1 自动摘要（opt-in，失败不影响入库）
    summary = _auto_summarize(markdown, title) if AUTO_SUMMARIZE else {}
    abstract = summary.get("abstract", "")
    overview = summary.get("overview", "")

    # 对 abstract 做 HTML comment sanitize：
    # - 去掉换行（multi-line comment 不影响 spec，但 grep/parser 可能误读）
    # - 替换 --> 避免提前关闭 <!-- ... --> 注释
    safe_abstract = abstract.replace("\n", " ").replace("-->", "→") if abstract else ""

    header = (
        f"<!-- curator_meta: ingested={today} freshness={freshness} ttl_days={ttl_days} -->\n"
        f"<!-- review_after: {(datetime.date.today() + datetime.timedelta(days=ttl_days)).isoformat()} -->\n"
    )
    if safe_abstract:
        header += f"<!-- abstract: {safe_abstract} -->\n"
    header += "\n"

    # L1 overview 作为 '## 摘要' section 前置，供 OV L1 层快速阅读
    overview_section = f"## 摘要\n\n{overview}\n\n---\n\n" if overview else ""
    full_content = header + overview_section + markdown

    # 写本地文件备份
    p = Path(CURATED_DIR)
    p.mkdir(parents=True, exist_ok=True)
    fn = p / f"{int(time.time())}_{re.sub(r'[^a-zA-Z0-9_-]+', '_', title)[:40]}.md"
    fn.write_text(full_content, encoding="utf-8")

    # 通过 backend 接口入库
    try:
        # 基础来源信息：优先使用显式传入 source_urls；未传(None)则从 markdown 里抽取 URL
        if source_urls is not None:
            extracted_urls = [u.strip() for u in source_urls if isinstance(u, str) and u.strip()]
        else:
            extracted_urls = re.findall(r"https?://[^\s)\]>\"']+", markdown or "")

        # 去重并保持顺序
        dedup_urls = []
        seen = set()
        for u in extracted_urls:
            if u not in seen:
                seen.add(u)
                dedup_urls.append(u)

        meta = {
            "freshness": freshness,
            "ttl_days": ttl_days,
            "ingested": today,
            "version": CURATOR_VERSION,
            "source_urls": dedup_urls,
            "quality_feedback": quality_feedback if isinstance(quality_feedback, dict) else {},
            "abstract": safe_abstract,  # L0 摘要（空字符串表示未生成；已 sanitize HTML comment 特殊字符）
        }

        uri = backend.ingest(full_content, title=title, metadata=meta)
        log.info("ingest_markdown_v2: 已入库 uri=%s, backup=%s", uri, fn)
        return {"root_uri": uri, "path": str(fn)}
    except Exception as e:
        log.warning("ingest_markdown_v2: 入库失败 (备份已写 %s): %s", fn, e)
        return {"status": "local_backup_only", "path": str(fn), "error": str(e)}
