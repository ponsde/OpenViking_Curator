"""Review: judge external results, ingest, conflict detection.

B2 优化：judge_and_ingest 合并审核+冲突检测为一次 LLM 调用。

All knowledge-store operations go through KnowledgeBackend; no direct OVClient use.
"""

from __future__ import annotations

import datetime
import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Sequence

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from .backend import KnowledgeBackend


# ── Judge prompt template loading ──

_JUDGE_PROMPT_TEMPLATE: str | None = None


def _load_judge_prompt() -> str | None:
    """Load judge prompt from external template file.

    Search order:
      1) CURATOR_JUDGE_PROMPT env var path
      2) curator/prompts/judge.prompt (package default)

    Returns None if no template found (falls back to built-in).
    """
    global _JUDGE_PROMPT_TEMPLATE
    if _JUDGE_PROMPT_TEMPLATE is not None:
        return _JUDGE_PROMPT_TEMPLATE

    candidates = []
    from .config import JUDGE_PROMPT_FILE

    env_path = (JUDGE_PROMPT_FILE or "").strip()
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path(__file__).parent / "prompts" / "judge.prompt")

    for p in candidates:
        if p.exists():
            try:
                _JUDGE_PROMPT_TEMPLATE = p.read_text(encoding="utf-8")
                return _JUDGE_PROMPT_TEMPLATE
            except Exception:
                pass

    return None


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
                return text[start : i + 1]

    return None


from .config import (
    AUTO_SUMMARIZE,
    CURATED_DIR,
    CURATOR_VERSION,
    JUDGE_MODEL,
    JUDGE_MODELS,
    OAI_BASE,
    OAI_KEY,
    SUMMARIZE_MODELS,
    chat,
    log,
)


def _is_transient_error(err: Exception) -> bool:
    """Return True for transient errors worth retrying (timeout, 429, 5xx).

    Permanent errors (4xx auth/validation) should not be retried.
    Mirrors the classification in config._should_retry_chat_error.
    """
    import requests

    if isinstance(err, requests.HTTPError):
        resp = getattr(err, "response", None)
        if resp is None:
            return True
        code = getattr(resp, "status_code", 0) or 0
        return code == 429 or code >= 500
    if isinstance(err, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(err, requests.RequestException):
        return True
    return False  # unknown exceptions (programming errors etc.) are not retried


def _parse_judge_output(raw_text: str | None, fallback_reason: str = "") -> JudgeResult:
    """Parse LLM output into a validated JudgeResult.

    Args:
        raw_text: Raw LLM response text (may contain JSON).
        fallback_reason: Reason string if parsing fails entirely.

    Returns:
        A validated :class:`JudgeResult`.
    """
    if raw_text is None:
        return JudgeResult.model_validate({"pass": False, "reason": fallback_reason or "no_response"})

    json_str = _extract_json(raw_text)
    if not json_str:
        return JudgeResult.model_validate({"pass": False, "reason": fallback_reason or "bad_json"})

    try:
        return JudgeResult.model_validate_json(json_str)
    except Exception as e:
        log.debug("model_validate_json failed, trying fallback: %s", e)
        # Fallback: try plain json.loads then construct
        try:
            data = json.loads(json_str)
            # Ensure conflict_points is a list
            if not isinstance(data.get("conflict_points"), list):
                data["conflict_points"] = []
            return JudgeResult.model_validate(data)
        except Exception as e:
            log.debug("judge output JSON parse fallback failed: %s", e)
            return JudgeResult.model_validate({"pass": False, "reason": fallback_reason or "json_parse_fail"})


def judge_and_ingest(
    backend: KnowledgeBackend, query: str, local_ctx: str, external_text: str, cv_warnings: Sequence[str] = ()
) -> dict:
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
        warnings_block = f"\n\n⚠️ cross_validate 风险标注（审核时必须考虑）:\n{joined}"

    # Load prompt: external template → built-in fallback
    template = _load_judge_prompt()
    if template:
        sys_prompt = template.replace("{today}", today).replace("{warnings_block}", warnings_block)
    else:
        sys_prompt = (
            "你是知识库治理助手。你需要同时完成两件事：\n\n"
            "1. **审核外搜结果**：判断是否值得入库\n"
            "   - 内容准确性、时效性、入库价值\n"
            "   - 超过1年未更新的标注[可能过时]\n"
            "   - 已取消/变更的功能当作当前事实 → pass=false\n\n"
            "2. **安全检查**：拒绝含以下内容的结果（pass=false）\n"
            "   - PII：邮箱、电话、身份证号、家庭地址等个人信息\n"
            "   - 有害内容：歧视、仇恨、暴力、非法活动指导\n\n"
            "3. **冲突检测**：比较本地知识与外搜结果是否有结论冲突\n"
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

    user_content = f"用户问题: {query}\n\n" f"本地知识:\n{local_snippet}\n\n" f"外搜结果:\n{external_snippet}"

    last_err = None
    out = None
    for jm in JUDGE_MODELS:
        try:
            out = chat(
                OAI_BASE,
                OAI_KEY,
                jm,
                [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_content},
                ],
                timeout=90,
            )
            break
        except Exception as e:
            last_err = e
            if not _is_transient_error(e):
                log.warning("judge: permanent error on model=%s: %s", jm, e)
                break
            log.debug("judge: transient error on model=%s, trying next: %s", jm, e)
            continue

    result = _parse_judge_output(out, fallback_reason=f"judge_fail:{last_err}")
    d = result.to_pipeline_dict()
    # Structured degradation flag: True when LLM call failed (not a content rejection)
    d["judge_degraded"] = out is None
    return d


_UNSAFE_HTML_RE = re.compile(
    r"<\s*(?:script|iframe|embed|object|applet|form|input|button)[^>]*>.*?</\s*(?:script|iframe|embed|object|applet|form|input|button)\s*>"
    r"|<\s*(?:script|iframe|embed|object|applet|form|input|button)[^>]*/?\s*>"
    r"|<\s*img\s+[^>]*(?:width|height)\s*=\s*[\"']?[01]\s*[\"']?[^>]*/?>"  # tracking pixels (0x0 or 1x1)
    r"|<\s*link\s+[^>]*rel\s*=\s*[\"']?(?:prefetch|preload|dns-prefetch)[\"']?[^>]*/?>",
    re.IGNORECASE | re.DOTALL,
)

# Matches on* event handler attributes (onerror, onclick, onload, …)
_ON_EVENT_RE = re.compile(r"""\s+on\w+\s*=\s*(?:"[^"]*"|'[^']*'|\S+)""", re.IGNORECASE)

# Matches dangerous URI schemes in href/src/action attributes
_DANGEROUS_PROTO_RE = re.compile(
    r"""((?:href|src|action)\s*=\s*)(["']?)\s*(?:javascript|data|vbscript)\s*:""",
    re.IGNORECASE,
)


def _sanitize_markdown(text: str) -> str:
    """Strip unsafe HTML tags, event handlers, and dangerous URIs from markdown.

    Removes: <script>, <iframe>, <embed>, <object>, <applet>, <form>,
    <input>, <button>, tracking pixels (0/1px images), prefetch links,
    on* event handler attributes, javascript:/data:/vbscript: URIs.
    """
    if not text:
        return text
    text = _UNSAFE_HTML_RE.sub("", text)
    text = _ON_EVENT_RE.sub("", text)
    text = _DANGEROUS_PROTO_RE.sub(r"\1\2#sanitized:", text)
    return text


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
            raw = chat(
                OAI_BASE,
                OAI_KEY,
                model,
                [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_content},
                ],
                timeout=30,
            )
            # 从 raw 里提取 JSON（括号深度匹配，比贪婪 regex 安全）
            json_str = _extract_json(raw)
            if not json_str:
                continue
            data = json.loads(json_str)
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


def ingest_markdown_v2(
    backend: KnowledgeBackend,
    title: str,
    markdown: str,
    freshness: str = "unknown",
    source_urls: list[str] | None = None,
    quality_feedback: dict | None = None,
    uri_hints: list[str] | None = None,
):
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
    base_ttl = ttl_map.get(freshness, 60)

    from .usage_ttl import compute_usage_ttl_for_ingest

    ttl_days, usage_tier_label = compute_usage_ttl_for_ingest(base_ttl, uri_hints or [])

    # Sanitize markdown: strip unsafe HTML (script, iframe, tracking pixels)
    markdown = _sanitize_markdown(markdown)

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
    import uuid

    ts = int(time.time())
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", title)[:40]
    fn = p / f"{ts}_{uuid.uuid4().hex[:8]}_{slug}.md"
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
            "usage_tier": usage_tier_label,
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
