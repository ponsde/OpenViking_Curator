"""Decision Report: 把 pipeline 决策路径转成人类可读摘要。

``format_report(result)`` 接收 ``pipeline_v2.run()`` 的返回值，
生成结构化的纯文本决策摘要，方便调试、日志输出和 CLI 展示。

设计原则：
- 无外部依赖，纯 Python
- 纯文本输出，终端友好（ASCII box）
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
    │ Ingested    : No                                     │
    └──────────────────────────────────────────────────────┘
"""

from __future__ import annotations

_WIDTH = 56  # 内容区宽度（不含边框）

_REASON_ZH = {
    "local_sufficient":  "本地知识库已足够，不触发外搜",
    "local_marginal":    "本地覆盖率边缘，触发外搜补充",
    "low_coverage":      "本地覆盖率低，触发外搜",
    "insufficient":      "本地知识库无匹配，触发外搜",
    "no_results":        "OV 无结果，触发外搜",
    "no_scores":         "OV 无分数结果，触发外搜",
    "not_evaluated":     "未评估",
}

_STAGE_ZH = {
    "L0": "L0 (abstract 已足够)",
    "L1": "L1 (摘要已足够)",
    "L2": "L2 (读取全文)",
    "none": "无内容加载",
}


def _row(label: str, value: str) -> str:
    """生成一行 │ label : value │，自动截断过长内容。"""
    line = f" {label:<12}: {value}"
    if len(line) > _WIDTH:
        line = line[:_WIDTH - 1] + "…"
    return f"│{line:<{_WIDTH}}│"


def format_report(result: dict) -> str:
    """将 pipeline_v2.run() 的返回值格式化为人类可读的决策摘要。

    Args:
        result: Return value of ``pipeline_v2.run()``.

    Returns:
        Multi-line string suitable for printing to stdout or logging.

    所有字段均有 fallback，result 为空 dict 时也能正常运行。
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
    conflict_label = conflict.get("summary", "") if has_conflict else "None"
    if has_conflict and not conflict_label:
        conflict_label = "Yes (no summary)"

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
        _row("Query",       f'"{query_display}"'),
        _row("Coverage",    f"{coverage:.2f}  ({cov_reason})"),
        _row("Reason",      cov_label),
        _row("Load stage",  stage_label),
        _row("Used URIs",   str(used_count)),
        _row("External",    external_label),
        _row("LLM calls",   str(llm_calls)),
        _row("Conflict",    conflict_label),
        _row("Ingested",    ingested_label),
        _row("Duration",    dur_label),
    ]
    if warnings:
        lines.append(_row("Warnings", warn_label))
    lines.append(footer)

    return "\n".join(lines)


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

    coverage   = meta.get("coverage", 0.0)
    cov_reason = meta.get("coverage_reason") or meta.get("external_reason") or "unknown"
    stage      = (meta.get("decision_trace") or {}).get("load_stage", "none")
    used       = len(meta.get("used_uris") or [])
    external   = "Yes" if meta.get("external_triggered") else "No"
    llm_calls  = trace.get("llm_calls", 0)
    has_conf   = "Yes" if conflict.get("has_conflict") else "No"

    return (
        f"[Curator] cov={coverage:.2f} ({cov_reason})"
        f" stage={stage} used={used}"
        f" ext={external} llm={llm_calls} conflict={has_conf}"
    )
