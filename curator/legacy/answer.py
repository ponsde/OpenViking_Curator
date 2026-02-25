"""Answer generation with source transparency."""

import json

from .config import (
    ANSWER_MODELS,
    OAI_BASE,
    OAI_KEY,
    chat,
    log,
)


def answer(query: str, local_ctx: str, external_ctx: str, priority_ctx: str = "",
           conflict_card: str = "", warnings: list = None):
    import datetime
    today = datetime.date.today().isoformat()
    warning_block = ""
    if warnings:
        warning_block = "\n⚠️ 以下信息需谨慎对待（可能过时或未经验证）:\n" + "\n".join([f"- {w}" for w in warnings[:5]])

    sys = (
        f"你是技术助手。当前日期: {today}。基于给定上下文回答。\n"
        "规则:\n"
        "1. 最后给来源列表，标注每个来源的日期\n"
        "2. 若存在冲突卡片，先展示冲突再给建议\n"
        "3. 对于不确定的信息，明确标注「⚠️ 待验证」\n"
        "4. 引用超过1年的资料时，提醒可能过时\n"
        "5. 区分「经过验证的事实」和「来自第三方项目的实现细节」\n"
        "6. 如果有警告信息，在回答开头提示用户注意"
    )
    user = (
        f"问题:\n{query}\n\n"
        f"{warning_block}\n\n"
        f"冲突卡片:\n{conflict_card}\n\n"
        f"优先来源上下文:\n{priority_ctx[:2500]}\n\n"
        f"本地上下文:\n{local_ctx[:5000]}\n\n"
        f"外部补充:\n{external_ctx[:3000]}"
    )

    last_err = None
    for m in ANSWER_MODELS:
        try:
            log.debug("answer_model_used=%s", m)
            return chat(OAI_BASE, OAI_KEY, m, [
                {"role": "system", "content": sys},
                {"role": "user", "content": user},
            ], timeout=90)
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"all answer models failed: {last_err}")


def _build_source_footer(meta: dict, coverage: float, external_used: bool,
                         warnings: list = None) -> str:
    """生成回答底部的来源透明度信息"""
    lines = ["---", "📊 **回答质量信息**"]

    # 覆盖率
    cov_pct = int(coverage * 100)
    if cov_pct >= 80:
        cov_label = "✅ 高"
    elif cov_pct >= 50:
        cov_label = "⚠️ 中等"
    else:
        cov_label = "❌ 低"
    lines.append(f"- 知识库覆盖率: {cov_pct}% ({cov_label})")
    lines.append(f"- 核心词覆盖: {meta.get('core_cov', '?')}")

    # 来源
    if external_used:
        lines.append("- 来源: 本地知识库 + 外部搜索（已交叉验证）")
    else:
        lines.append("- 来源: 本地知识库")

    # 使用的资源
    uris = meta.get('priority_uris', [])
    if uris:
        short_uris = [u.split('/')[-1].replace('.md', '') for u in uris[:3]]
        lines.append(f"- 主要参考: {', '.join(short_uris)}")

    # 警告
    if warnings:
        lines.append(f"- ⚠️ 有 {len(warnings)} 条待验证信息")

    # 反馈入口
    lines.append("")
    lines.append("💬 对这个回答满意吗？反馈帮助改善未来回答质量。")

    return "\n".join(lines)


