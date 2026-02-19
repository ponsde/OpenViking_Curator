"""Review: judge external results, ingest, conflict detection."""

import json
import re
import time
import datetime
from pathlib import Path

from .config import (
    log, chat,
    OAI_BASE, OAI_KEY, JUDGE_MODEL, JUDGE_MODELS, ANSWER_MODELS, CURATED_DIR, _GENERIC_TERMS,
)

def judge_and_pack(query: str, external_text: str):
    import datetime
    today = datetime.date.today().isoformat()
    sys = (
        "你是资料审核器。判断外部搜索结果是否值得入库。\n"
        f"当前日期: {today}\n\n"
        "审核维度:\n"
        "1. 内容准确性 — 信息是否正确、是否有来源支撑\n"
        "2. 时效性 — 信息是否仍然有效？API流程/注册方式/技术要求等易变内容尤其注意\n"
        "   - 超过1年未更新的项目信息：trust降低，标注[可能过时]\n"
        "   - 引用已取消/变更的功能当作当前事实：pass=false\n"
        "   - 将旧版本要求（如已取消的手机验证）当成现行要求：pass=false\n"
        "3. 入库价值 — 是否值得长期保存，还是只是临时参考\n\n"
        "输出严格JSON: pass(bool), reason(string), tags(array), trust(0-10), "
        "freshness(string: current/recent/outdated/unknown), "
        "summary(string), markdown(string)。\n"
        "markdown要求包含来源URL和信息日期。只输出JSON。"
    )

    last_err = None
    out = None
    for jm in JUDGE_MODELS:
        try:
            log.debug("judge_model_used=%s", jm)
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

    m = re.search(r"\{[\s\S]*\}", out)
    if not m:
        return {"pass": False, "reason": "bad_json", "tags": [], "trust": 0, "summary": "", "markdown": ""}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {"pass": False, "reason": "json_parse_fail", "tags": [], "trust": 0, "summary": "", "markdown": ""}


def ingest_markdown(client, title: str, markdown: str, freshness: str = "unknown"):
    import datetime
    p = Path(CURATED_DIR)
    p.mkdir(parents=True, exist_ok=True)

    # P2: 入库时写入 metadata（日期 + 时效标签）
    today = datetime.date.today().isoformat()
    ttl_map = {"current": 180, "recent": 90, "unknown": 60, "outdated": 0}
    ttl_days = ttl_map.get(freshness, 60)

    header = (
        f"<!-- curator_meta: ingested={today} freshness={freshness} ttl_days={ttl_days} -->\n"
        f"<!-- review_after: {(datetime.date.today() + datetime.timedelta(days=ttl_days)).isoformat()} -->\n\n"
    )

    fn = p / f"{int(time.time())}_{re.sub(r'[^a-zA-Z0-9_-]+', '_', title)[:40]}.md"
    fn.write_text(header + markdown, encoding="utf-8")
    ing = client.add_resource(path=str(fn))

    # 关键修复：入库后等待语义索引完成，否则下一次检索拿不到新文档
    try:
        uri = ing.get("root_uri", "") if isinstance(ing, dict) else ""
        if uri:
            client.wait_processed()  # 不传参：等全部队列完成
    except Exception:
        pass

    return ing


def build_priority_context(client, uris, query: str = ""):
    """读取优先资源内容。如果提供 query，用核心词验证相关性，过滤不相关文档。"""
    blocks = []
    # 核心词验证：如果提供了 query，只保留内容中包含核心词的文档
    if query:
        q_core = set(re.findall(r"[a-zA-Z0-9_\-]{3,}", query.lower())) - _GENERIC_TERMS
        q_cn = set(re.findall(r"[\u4e00-\u9fff]{2,4}", query))
        check_terms = q_core | q_cn
    else:
        check_terms = set()

    for u in uris[:4]:  # 多看几个，过滤后可能不够
        try:
            c = str(client.read(u))[:1500]
            # 语义过滤：核心词至少命中1个才算相关
            if check_terms:
                c_lower = c.lower()
                hits = sum(1 for t in check_terms if t.lower() in c_lower)
                if hits == 0:
                    continue
            blocks.append(f"[PRIORITY_SOURCE] {u}\n{c[:1200]}")
            if len(blocks) >= 2:
                break
        except Exception:
            continue
    return "\n\n".join(blocks)


def detect_conflict(query: str, local_ctx: str, external_ctx: str):
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
    m = re.search(r"\{[\s\S]*\}", out)
    if not m:
        return {"has_conflict": False, "summary": "", "points": []}
    try:
        j = json.loads(m.group(0))
        if "points" not in j or not isinstance(j.get("points"), list):
            j["points"] = []
        return j
    except Exception:
        return {"has_conflict": False, "summary": "", "points": []}


def ingest_markdown_v2(ov_client, title: str, markdown: str, freshness: str = "unknown"):
    """通过 OV HTTP API 入库（不依赖 SyncOpenViking）。"""
    import datetime

    p = Path(CURATED_DIR)
    p.mkdir(parents=True, exist_ok=True)

    today = datetime.date.today().isoformat()
    ttl_map = {"current": 180, "recent": 90, "unknown": 60, "outdated": 0}
    ttl_days = ttl_map.get(freshness, 60)

    header = (
        f"<!-- curator_meta: ingested={today} freshness={freshness} ttl_days={ttl_days} -->\n"
        f"<!-- review_after: {(datetime.date.today() + datetime.timedelta(days=ttl_days)).isoformat()} -->\n\n"
    )

    fn = p / f"{int(time.time())}_{re.sub(r'[^a-zA-Z0-9_-]+', '_', title)[:40]}.md"
    fn.write_text(header + markdown, encoding="utf-8")

    # 通过 HTTP API 入库
    result = ov_client.add_resource(str(fn), reason="curator_ingest")
    return result
