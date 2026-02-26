"""Decision Report: 把 pipeline 决策路径转成多种格式摘要。

接收 ``pipeline_v2.run()`` 的返回值，支持四种输出格式：

- :func:`format_report` — ASCII box，终端友好（CJK 安全）
- :func:`format_report_short` — 单行紧凑摘要，适合日志
- :func:`format_report_json` — JSON 字符串，适合 API / Loki
- :func:`format_report_html` — HTML 片段，适合 Web UI 嵌入

设计原则：
- 无外部依赖，纯 Python（unicodedata、json、html 均为标准库）
- 所有字段有默认值，result 结构不完整时不报错
- 信息密度 > 装饰

输出示例：

    ┌─── Curator Decision Report ──────────────────────────┐
    │ Query       : "docker compose 如何配置 healthcheck"   │
    │ Coverage    : 0.72  (local_sufficient)               │
    │ Load stage  : L0                                     │
    │ Used URIs   : 2                                      │
    │ External    : No                                     │
    │ LLM calls   : 0                                      │
    │ Conflict    : None                                   │
    └──────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import html as _html
import json
import unicodedata

_WIDTH = 56  # 内容区显示列宽（不含边框 │ 字符）

_REASON_ZH = {
    "local_sufficient": "本地知识库已足够，不触发外搜",
    "local_marginal": "本地覆盖率边缘，触发外搜补充",
    "low_coverage": "本地覆盖率低，触发外搜",
    "insufficient": "本地知识库无匹配，触发外搜",
    "no_results": "OV 无结果，触发外搜",
    "no_scores": "OV 无分数结果，触发外搜",
    "not_evaluated": "未评估",
}

_STAGE_ZH = {
    "L0": "L0 (abstract 已足够)",
    "L1": "L1 (摘要已足够)",
    "L2": "L2 (读取全文)",
    "none": "无内容加载",
}


def _display_width(s: str) -> int:
    """计算字符串的终端显示宽度（CJK 宽字符占 2 列，其余占 1 列）。"""
    w = 0
    for ch in s:
        eaw = unicodedata.east_asian_width(ch)
        w += 2 if eaw in ("W", "F") else 1
    return w


def _pad_to(s: str, width: int) -> str:
    """右填充空格，使显示宽度达到 width。CJK 安全版。"""
    dw = _display_width(s)
    if dw < width:
        s += " " * (width - dw)
    return s


def _truncate_to(s: str, max_width: int) -> str:
    """截断字符串，使显示宽度不超过 max_width（CJK 安全）。

    若需要截断，末尾加 '…'（占 1 列）。
    Exact-fit（字符串宽度恰好等于 max_width）时直接返回原字符串，不截断。
    """
    cur = 0
    for i, ch in enumerate(s):
        eaw = unicodedata.east_asian_width(ch)
        step = 2 if eaw in ("W", "F") else 1
        if cur + step > max_width:
            # 当前字符放不下（即使没有 '…'），截断并加省略号
            return s[:i] + "…"
        cur += step
        if cur == max_width and i < len(s) - 1:
            # 恰好填满，但后面还有字符——用省略号替换当前字符
            return s[:i] + "…"
    return s


def _row(label: str, value: str) -> str:
    """生成一行 │ label : value │，正确处理 CJK 字符宽度。"""
    inner = f" {label:<12}: {value}"
    # 计算显示宽度，超出则截断（CJK 安全）
    if _display_width(inner) > _WIDTH:
        inner = _truncate_to(inner, _WIDTH)
    # 右填充到 _WIDTH 显示列
    inner = _pad_to(inner, _WIDTH)
    return f"│{inner}│"


def format_report(result: dict) -> str:
    """将 pipeline_v2.run() 的返回值格式化为人类可读的决策摘要。

    Args:
        result: Return value of ``pipeline_v2.run()``.

    Returns:
        Multi-line string suitable for printing to stdout or logging.

    所有字段均有 fallback，result 为空 dict 时也能正常运行。
    CJK 字符（中日韩）按 2 列宽处理，box 宽度对齐正确。
    """
    meta = result.get("meta") or {}
    trace = meta.get("decision_trace") or {}
    metrics = result.get("metrics") or {}
    conflict = result.get("conflict") or {}

    # ── 字段提取 ────────────────────────────────────────────
    query = str(result.get("query") or "")
    query_display = query[:50] + "…" if len(query) > 50 else query

    coverage = meta.get("coverage", 0.0)
    cov_reason = meta.get("coverage_reason") or meta.get("external_reason") or "unknown"
    cov_label = _REASON_ZH.get(cov_reason, cov_reason)

    load_stage = trace.get("load_stage", "none")
    stage_label = _STAGE_ZH.get(load_stage, load_stage)

    used_uris = meta.get("used_uris") or []
    used_count = len(used_uris)

    external = meta.get("external_triggered", False)
    external_label = "Yes" if external else "No"

    llm_calls = trace.get("llm_calls", 0)

    has_conflict = conflict.get("has_conflict", False)
    if has_conflict:
        conflict_label = conflict.get("summary", "") or "Yes (no summary)"
    else:
        conflict_label = "None"

    cache_hit = (metrics.get("flags") or {}).get("cache_hit")
    if cache_hit is True:
        cache_label = "Hit"
    elif cache_hit is False:
        cache_label = "Miss"
    else:
        cache_label = "N/A"

    ingested = meta.get("ingested", False)
    ingested_label = "Yes" if ingested else "No"

    dur = metrics.get("duration_sec")
    dur_label = f"{dur:.1f}s" if isinstance(dur, (int, float)) else "—"

    warnings = meta.get("warnings") or []
    warn_label = "; ".join(str(w) for w in warnings[:2]) if warnings else "None"

    # ── 拼装报告 ────────────────────────────────────────────
    border = "─" * (_WIDTH + 2)
    title = " Curator Decision Report "
    pad_left = (_WIDTH - len(title)) // 2
    pad_right = _WIDTH - len(title) - pad_left
    header = f"┌{'─' * pad_left}{title}{'─' * pad_right}┐"
    footer = f"└{border[1:-1]}┘"

    lines = [
        header,
        _row("Query", f'"{query_display}"'),
        _row("Coverage", f"{coverage:.2f}  ({cov_reason})"),
        _row("Reason", cov_label),
        _row("Load stage", stage_label),
        _row("Used URIs", str(used_count)),
        _row("External", external_label),
        _row("Cache", cache_label),
        _row("LLM calls", str(llm_calls)),
        _row("Conflict", conflict_label),
        _row("Ingested", ingested_label),
        _row("Duration", dur_label),
    ]
    if warnings:
        lines.append(_row("Warnings", warn_label))
    lines.append(footer)

    return "\n".join(lines)


def _extract_report_fields(result: dict) -> dict:
    """Extract all decision fields from a pipeline result into a plain dict.

    Used by :func:`format_report_json` and :func:`format_report_html`.
    All keys always present; missing source fields fall back to safe defaults.
    """
    meta = result.get("meta") or {}
    trace = meta.get("decision_trace") or {}
    metrics = result.get("metrics") or {}
    conflict = result.get("conflict") or {}
    flags = metrics.get("flags") or {}

    return {
        "query": str(result.get("query") or ""),
        "run_id": str(result.get("run_id") or ""),
        "coverage": float(meta.get("coverage") or 0.0),
        "coverage_reason": str(meta.get("coverage_reason") or meta.get("external_reason") or "unknown"),
        "load_stage": str(trace.get("load_stage") or "none"),
        "used_uris": list(meta.get("used_uris") or []),
        "external_triggered": bool(meta.get("external_triggered", False)),
        "cache_hit": flags.get("cache_hit"),  # True / False / None
        "llm_calls": int(trace.get("llm_calls") or 0),
        "has_conflict": bool(conflict.get("has_conflict", False)),
        "conflict_summary": str(conflict.get("summary") or ""),
        "ingested": bool(meta.get("ingested", False)),
        "duration_sec": metrics.get("duration_sec"),  # float or None
        "warnings": list(meta.get("warnings") or []),
    }


def format_report_short(result: dict) -> str:
    """单行紧凑摘要，适合日志写入和 metrics 追踪。

    格式：
        [Curator] cov=0.72 (local_sufficient) stage=L0 used=2 ext=No llm=0 conflict=No

    Args:
        result: Return value of ``pipeline_v2.run()``.

    Returns:
        Single-line string.
    """
    meta = result.get("meta") or {}
    trace = meta.get("decision_trace") or {}
    conflict = result.get("conflict") or {}

    coverage = meta.get("coverage", 0.0)
    cov_reason = meta.get("coverage_reason") or meta.get("external_reason") or "unknown"
    stage = trace.get("load_stage", "none")
    used = len(meta.get("used_uris") or [])
    external = "Yes" if meta.get("external_triggered") else "No"
    llm_calls = trace.get("llm_calls", 0)
    has_conf = "Yes" if conflict.get("has_conflict") else "No"

    return (
        f"[Curator] cov={coverage:.2f} ({cov_reason})"
        f" stage={stage} used={used}"
        f" ext={external} llm={llm_calls} conflict={has_conf}"
    )


def format_report_json(result: dict) -> str:
    """Return decision report as a JSON string.

    Suitable for structured logging (Loki), API responses, and machine
    consumption. All field names use snake_case; missing values fall back
    to safe defaults.

    Args:
        result: Return value of ``pipeline_v2.run()``.

    Returns:
        Indented JSON string (``ensure_ascii=False``).
    """
    f = _extract_report_fields(result)
    payload = {
        "query": f["query"],
        "run_id": f["run_id"],
        "coverage": round(f["coverage"], 4),
        "coverage_reason": f["coverage_reason"],
        "load_stage": f["load_stage"],
        "used_uris_count": len(f["used_uris"]),
        "external_triggered": f["external_triggered"],
        "cache_hit": f["cache_hit"],
        "llm_calls": f["llm_calls"],
        "has_conflict": f["has_conflict"],
        "conflict_summary": f["conflict_summary"],
        "ingested": f["ingested"],
        "duration_sec": f["duration_sec"],
        "warnings": [str(w) for w in f["warnings"]],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def format_report_html(result: dict) -> str:
    """Return decision report as a self-contained HTML fragment.

    Produces a ``<div class="curator-decision-report">`` containing a
    plain ``<table>`` — no external CSS or JS dependencies.  Safe for
    embedding in dashboards, Jupyter notebooks, or email reports.

    Args:
        result: Return value of ``pipeline_v2.run()``.

    Returns:
        HTML string fragment (not a full document).
    """
    f = _extract_report_fields(result)

    def _tr(label: str, value: str) -> str:
        return (
            f"  <tr>"
            f"<th style='text-align:left;padding:2px 8px'>{_html.escape(label)}</th>"
            f"<td style='padding:2px 8px'>{_html.escape(value)}</td>"
            f"</tr>"
        )

    cache_label = "Hit" if f["cache_hit"] is True else "Miss" if f["cache_hit"] is False else "N/A"
    conflict_label = f["conflict_summary"] or ("Yes" if f["has_conflict"] else "None")
    dur_label = f"{f['duration_sec']:.1f}s" if isinstance(f["duration_sec"], (int, float)) else "—"
    stage_label = _STAGE_ZH.get(f["load_stage"], f["load_stage"])

    rows = [
        _tr("Query", f["query"][:100]),
        _tr("Run ID", f["run_id"]),
        _tr("Coverage", f"{f['coverage']:.2f} ({f['coverage_reason']})"),
        _tr("Load Stage", stage_label),
        _tr("Used URIs", str(len(f["used_uris"]))),
        _tr("External", "Yes" if f["external_triggered"] else "No"),
        _tr("Cache", cache_label),
        _tr("LLM Calls", str(f["llm_calls"])),
        _tr("Conflict", conflict_label),
        _tr("Ingested", "Yes" if f["ingested"] else "No"),
        _tr("Duration", dur_label),
    ]
    if f["warnings"]:
        rows.append(_tr("Warnings", "; ".join(str(w) for w in f["warnings"][:3])))

    inner = "\n".join(rows)
    return (
        '<div class="curator-decision-report">\n'
        '<table style="border-collapse:collapse;font-family:monospace;font-size:13px">\n'
        f"{inner}\n"
        "</table>\n"
        "</div>"
    )
