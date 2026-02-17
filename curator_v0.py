#!/usr/bin/env python3
import os
import json
import re
import time
from pathlib import Path

import requests
import openviking as ov
from metrics import Metrics
from memory_capture import capture_case

"""
OpenViking Curator v0 (pilot)

Security:
- NO hardcoded API keys
- All secrets loaded from environment variables
"""


def env(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v.strip() if isinstance(v, str) else v


# ---- Config from env ----
OPENVIKING_CONFIG_FILE = env("OPENVIKING_CONFIG_FILE", str(Path.home() / ".openviking" / "ov.conf"))
DATA_PATH = env("CURATOR_DATA_PATH", str(Path.cwd() / "data"))
CURATED_DIR = env("CURATOR_CURATED_DIR", str(Path.cwd() / "curated"))

OAI_BASE = env("CURATOR_OAI_BASE")  # e.g. https://oai.whidsm.cn/v1
OAI_KEY = env("CURATOR_OAI_KEY")

ROUTER_MODELS = [
    m.strip() for m in env(
        "CURATOR_ROUTER_MODELS",
        "gemini-3-flash-preview,gemini-3-flash-high,【Claude Code】Claude-Sonnet 4-5",
    ).split(",") if m.strip()
]
JUDGE_MODEL = env("CURATOR_JUDGE_MODEL", "【Claude Code】Claude-Sonnet 4-5")

GROK_BASE = env("CURATOR_GROK_BASE", "http://127.0.0.1:8000/v1")
GROK_KEY = env("CURATOR_GROK_KEY")
GROK_MODEL = env("CURATOR_GROK_MODEL", "grok-4-fast")


def validate_config() -> None:
    missing = []
    if not OAI_BASE:
        missing.append("CURATOR_OAI_BASE")
    if not OAI_KEY:
        missing.append("CURATOR_OAI_KEY")
    if not GROK_KEY:
        missing.append("CURATOR_GROK_KEY")
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")


def chat(base, key, model, messages, timeout=60):
    r = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "stream": False},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def route_scope(query: str):
    sys = (
        "你是检索路由器。把用户问题转换为严格JSON，字段: "
        "domain(字符串), keywords(数组), exclude(数组), need_fresh(boolean), source_pref(数组), confidence(0-1)。"
        "只输出JSON，不要解释。"
    )
    last_err = None
    out = None
    chosen = None
    for m in ROUTER_MODELS:
        try:
            out = chat(OAI_BASE, OAI_KEY, m, [
                {"role": "system", "content": sys},
                {"role": "user", "content": query},
            ], timeout=45)
            chosen = m
            break
        except Exception as e:
            last_err = e
            continue
    if out is None:
        raise RuntimeError(f"all router models failed: {last_err}")
    print(f"router_model_used={chosen}")

    m = re.search(r"\{[\s\S]*\}", out)
    if not m:
        return {
            "domain": "general",
            "keywords": [query],
            "exclude": [],
            "need_fresh": True,
            "source_pref": ["official docs", "github"],
            "confidence": 0.5,
        }
    try:
        return json.loads(m.group(0))
    except Exception:
        return {
            "domain": "general",
            "keywords": [query],
            "exclude": [],
            "need_fresh": True,
            "source_pref": ["official docs", "github"],
            "confidence": 0.5,
        }


def load_feedback(path: str):
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            return {}
    return {}


def uri_feedback_score(uri: str, fb: dict) -> int:
    if not isinstance(fb, dict):
        return 0

    def _score(item):
        up = int(item.get('up', 0))
        down = int(item.get('down', 0))
        adopt = int(item.get('adopt', 0))
        return up - down + adopt * 2

    # exact match
    if uri in fb:
        return _score(fb[uri])

    # fuzzy match: same subtree / parent-child path overlap
    best = 0
    for k, v in fb.items():
        if not isinstance(k, str):
            continue
        if k in uri or uri in k:
            best = max(best, _score(v))
    return best


def uri_trust_score(uri: str) -> float:
    u = (uri or '').lower()
    s = 5.0
    if 'openviking' in u or 'grok2api' in u or 'newapi' in u:
        s += 1.0
    if 'curated' in u:
        s += 0.5
    if 'license' in u:
        s -= 0.5
    return s


def uri_freshness_score(uri: str) -> float:
    # very light heuristic: curated entries likely newer
    u = (uri or '').lower()
    return 1.0 if 'curated' in u else 0.0


def build_feedback_priority_uris(uris, feedback_file='feedback.json', topn=3):
    fb = load_feedback(feedback_file)
    scored = []
    seen = set()
    for u in uris:
        if u in seen:
            continue
        seen.add(u)
        f = uri_feedback_score(u, fb)             # strong user signal
        t = uri_trust_score(u)                    # weak prior
        r = uri_freshness_score(u)                # freshness prior
        final = 0.50 * f + 0.30 * t + 0.20 * r
        scored.append((final, f, t, r, u))
    scored.sort(reverse=True, key=lambda x: x[0])
    return [x[4] for x in scored[:topn]], scored[:min(5, len(scored))]


def local_search(client, query: str, scope: dict):
    expanded = query + "\n关键词:" + ",".join(scope.get("keywords", [])[:8])
    res = client.search(expanded)
    txt = str(res)
    txt_l = txt.lower()

    kw = scope.get("keywords", [])[:6]
    hit = sum(1 for k in kw if isinstance(k, str) and k and k.lower() in txt_l)
    kw_cov = hit / max(1, len(kw))

    target_terms = []
    ql = query.lower()
    if "grok2api" in ql:
        target_terms += ["grok2api", "curated"]
    if "openviking" in ql:
        target_terms += ["openviking"]
    if "newapi" in ql:
        target_terms += ["newapi", "oneapi"]

    uris = re.findall(r"uri='([^']+)'", txt)
    uris_l = " ".join(uris).lower()
    domain_hit = any(t in uris_l for t in target_terms) if target_terms else True

    coverage = kw_cov if domain_hit else min(kw_cov, 0.15)

    # 4) feedback 调权（v0.2）：adopt/up/down 影响覆盖率与外搜触发
    fb = load_feedback(os.getenv('CURATOR_FEEDBACK_FILE', 'feedback.json'))
    uri_scores = {u: uri_feedback_score(u, fb) for u in uris[:20]}
    max_fb = max(uri_scores.values()) if uri_scores else 0
    if max_fb > 0:
        coverage = min(1.0, coverage + 0.1 * max_fb)

    pri_uris, rank_preview = build_feedback_priority_uris(uris, os.getenv('CURATOR_FEEDBACK_FILE', 'feedback.json'), topn=3)

    return txt, coverage, {
        "kw_cov": kw_cov,
        "domain_hit": domain_hit,
        "target_terms": target_terms,
        "uris": uris[:8],
        "max_feedback_score": max_fb,
        "priority_uris": pri_uris,
        "rank_preview": rank_preview,
    }


def external_search(query: str, scope: dict):
    prompt = (
        f"问题: {query}\n"
        f"关键词: {scope.get('keywords', [])}\n"
        f"排除: {scope.get('exclude', [])}\n"
        f"偏好来源: {scope.get('source_pref', [])}\n"
        "请搜索并返回5条高质量来源，格式：标题+URL+关键点。"
    )
    return chat(GROK_BASE, GROK_KEY, GROK_MODEL, [
        {"role": "system", "content": "你是实时搜索助手，重视可验证来源。"},
        {"role": "user", "content": prompt},
    ], timeout=90)


def judge_and_pack(query: str, external_text: str):
    sys = (
        "你是资料审核器。判断外部搜索结果是否值得入库。"
        "输出严格JSON: pass(bool), reason(string), tags(array), trust(0-10), summary(string), markdown(string)。"
        "markdown要求包含来源URL。只输出JSON。"
    )
    out = chat(OAI_BASE, OAI_KEY, JUDGE_MODEL, [
        {"role": "system", "content": sys},
        {"role": "user", "content": f"用户问题:{query}\n候选资料:\n{external_text}"},
    ], timeout=90)
    m = re.search(r"\{[\s\S]*\}", out)
    if not m:
        return {"pass": False, "reason": "bad_json", "tags": [], "trust": 0, "summary": "", "markdown": ""}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {"pass": False, "reason": "json_parse_fail", "tags": [], "trust": 0, "summary": "", "markdown": ""}


def ingest_markdown(client, title: str, markdown: str):
    p = Path(CURATED_DIR)
    p.mkdir(parents=True, exist_ok=True)
    fn = p / f"{int(time.time())}_{re.sub(r'[^a-zA-Z0-9_-]+', '_', title)[:40]}.md"
    fn.write_text(markdown, encoding="utf-8")
    return client.add_resource(path=str(fn))


def build_priority_context(client, uris):
    blocks = []
    for u in uris[:2]:
        try:
            c = client.read(u)
            blocks.append(f"[PRIORITY_SOURCE] {u}\n{str(c)[:1200]}")
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


def answer(query: str, local_ctx: str, external_ctx: str, priority_ctx: str = "", conflict_card: str = ""):
    sys = "你是技术助手。基于给定上下文回答，最后给来源列表。若存在冲突卡片，先展示冲突再给建议。"
    user = f"问题:\n{query}\n\n冲突卡片:\n{conflict_card}\n\n优先来源上下文:\n{priority_ctx[:2500]}\n\n本地上下文:\n{local_ctx[:5000]}\n\n外部补充:\n{external_ctx[:3000]}"
    return chat(OAI_BASE, OAI_KEY, JUDGE_MODEL, [
        {"role": "system", "content": sys},
        {"role": "user", "content": user},
    ], timeout=90)


def run(query: str):
    m = Metrics()
    validate_config()
    os.environ["OPENVIKING_CONFIG_FILE"] = OPENVIKING_CONFIG_FILE

    print("STEP 1/6 初始化...")
    client = ov.SyncOpenViking(path=DATA_PATH)
    client.initialize()
    m.step('init', True)
    print("✅ STEP 1 完成")

    try:
        print("STEP 2/6 范围路由...")
        scope = route_scope(query)
        m.step('route', True, {'domain': scope.get('domain'), 'confidence': scope.get('confidence')})
        m.score('router_confidence', scope.get('confidence', 0))
        print("✅ STEP 2 完成:", json.dumps(scope, ensure_ascii=False))

        print("STEP 3/6 本地检索(OpenViking)...")
        local_txt, coverage, meta = local_search(client, query, scope)
        m.step('local_search', True, {'coverage': coverage, 'kw_cov': meta.get('kw_cov'), 'domain_hit': meta.get('domain_hit')})
        m.score('coverage_before_external', round(coverage, 3))
        print(
            f"✅ STEP 3 完成: coverage={coverage:.2f}, kw_cov={meta['kw_cov']:.2f}, "
            f"domain_hit={meta['domain_hit']}, fb_max={meta.get('max_feedback_score',0)}, "
            f"priority_uris={meta.get('priority_uris',[])}, rank_preview={meta.get('rank_preview',[])}, "
            f"target_terms={meta['target_terms']}, uris={meta.get('uris', [])}"
        )

        external_txt = ""
        ingested = False
        if coverage < 0.65:
            m.flag('external_triggered', True)
            print("STEP 4/6 覆盖不足，触发外部搜索(Grok)...")
            external_txt = external_search(query, scope)
            m.step('external_search', True, {'len': len(external_txt)})
            print("✅ STEP 4 完成: 外部结果长度", len(external_txt))

            print("STEP 5/6 审核并尝试入库...")
            j = judge_and_pack(query, external_txt)
            m.step('judge', True, {'pass': j.get('pass'), 'trust': j.get('trust')})
            print("审核结果:", json.dumps({k: j.get(k) for k in ["pass", "reason", "trust", "tags"]}, ensure_ascii=False))
            if j.get("pass") and j.get("markdown"):
                ing = ingest_markdown(client, "curated", j["markdown"])
                ingested = True
                m.step('ingest', True, {'uri': ing.get('root_uri', '')})
                print("✅ 已入库:", ing.get("root_uri", ""))
            else:
                m.step('ingest', False)
                print("⚠️ 未入库")
        else:
            m.flag('external_triggered', False)
            print("STEP 4/6 跳过外部搜索（本地覆盖足够）")

        print("STEP 6/7 冲突检测...")
        conflict = detect_conflict(query, local_txt, external_txt)
        conflict_card = ""
        if conflict.get('has_conflict'):
            pts = '\n'.join([f"- {x}" for x in conflict.get('points', [])[:5]])
            conflict_card = f"⚠️ 存在冲突: {conflict.get('summary','')}\n{pts}"
        m.step('conflict', True, {'has_conflict': conflict.get('has_conflict', False), 'summary': conflict.get('summary','')})
        m.flag('has_conflict', bool(conflict.get('has_conflict', False)))
        print(f"✅ STEP 6 完成: has_conflict={bool(conflict.get('has_conflict', False))}")

        print("STEP 7/7 生成回答...")
        priority_ctx = build_priority_context(client, meta.get('priority_uris', []))
        ans = answer(query, local_txt, external_txt, priority_ctx=priority_ctx, conflict_card=conflict_card)
        m.step('answer', True, {'answer_len': len(ans), 'priority_uris': meta.get('priority_uris', [])})
        m.score('priority_uris_count', len(meta.get('priority_uris', [])))
        m.flag('ingested', ingested)
        m.score('answer_len', len(ans))
        report = m.finalize()

        case_path = None
        if os.getenv('CURATOR_CAPTURE_CASE', '1') in ('1','true','True'):
            case_path = capture_case(query, scope, report, ans, out_dir=os.getenv('CURATOR_CASE_DIR','cases'))

        print("\n===== FINAL ANSWER =====\n")
        print(ans)
        print("\n===== EVAL METRICS =====\n")
        print(json.dumps({
            'duration_sec': report['duration_sec'],
            'flags': report['flags'],
            'scores': report['scores'],
            'case_path': case_path
        }, ensure_ascii=False, indent=2))
    finally:
        try:
            client.close()
        except Exception:
            pass


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]).strip() or "grok2api 自动注册需要哪些前置配置和常见失败原因？"
    run(q)
