#!/usr/bin/env python3
import os, json, re, time, tempfile
from pathlib import Path
import requests
import openviking as ov

# ---- Config ----
OPENVIKING_CONFIG_FILE = '/home/ponsde/OpenViking_test/ov.conf'
DATA_PATH = '/home/ponsde/OpenViking_test/data'
OAI_BASE = 'https://oai.whidsm.cn/v1'
OAI_KEY = '<REDACTED_OAI_KEY>'
ROUTER_MODELS = ['gemini-3-flash-preview', 'gemini-3-flash-high', '【Claude Code】Claude-Sonnet 4-5']
JUDGE_MODEL = '【Claude Code】Claude-Sonnet 4-5'
GROK_BASE = 'http://127.0.0.1:8000/v1'
GROK_KEY = '<REDACTED_GROK_KEY>'
GROK_MODEL = 'grok-4-fast'


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


def local_search(client, query: str, scope: dict):
    expanded = query + "\n关键词:" + ",".join(scope.get("keywords", [])[:8])
    res = client.search(expanded)
    txt = str(res)
    txt_l = txt.lower()

    # 1) 关键词命中（基础分）
    kw = scope.get("keywords", [])[:6]
    hit = sum(1 for k in kw if isinstance(k, str) and k and k.lower() in txt_l)
    kw_cov = hit / max(1, len(kw))

    # 2) 目标域命中（强约束，基于命中URI而不是全文）
    target_terms = []
    ql = query.lower()
    if 'grok2api' in ql:
        target_terms += ['tmpy5z07k6q', 'grok2api']
    if 'openviking' in ql:
        target_terms += ['tmprg3jw36b', 'tmppuozobid', 'tmpv65n2y0x', 'openviking']
    if 'newapi' in ql:
        target_terms += ['newapi', 'oneapi']

    # 从检索结果里提取 uri='...'
    uris = re.findall(r"uri='([^']+)'", txt)
    uris_l = ' '.join(uris).lower()
    domain_hit = any(t in uris_l for t in target_terms) if target_terms else True

    # 3) 组合覆盖率：域命中不过关则强制低覆盖，触发外搜
    coverage = kw_cov if domain_hit else min(kw_cov, 0.15)
    return txt, coverage, {'kw_cov': kw_cov, 'domain_hit': domain_hit, 'target_terms': target_terms, 'uris': uris[:8]}


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
    p = Path('/home/ponsde/OpenViking_test/curated')
    p.mkdir(parents=True, exist_ok=True)
    fn = p / f"{int(time.time())}_{re.sub(r'[^a-zA-Z0-9_-]+','_',title)[:40]}.md"
    fn.write_text(markdown, encoding='utf-8')
    return client.add_resource(path=str(fn))


def answer(query: str, local_ctx: str, external_ctx: str):
    sys = "你是技术助手。基于给定上下文回答，最后给来源列表。"
    user = f"问题:\n{query}\n\n本地上下文:\n{local_ctx[:5000]}\n\n外部补充:\n{external_ctx[:3000]}"
    return chat(OAI_BASE, OAI_KEY, JUDGE_MODEL, [
        {"role": "system", "content": sys},
        {"role": "user", "content": user},
    ], timeout=90)


def run(query: str):
    os.environ['OPENVIKING_CONFIG_FILE'] = OPENVIKING_CONFIG_FILE
    print('STEP 1/6 初始化...')
    client = ov.SyncOpenViking(path=DATA_PATH)
    client.initialize()
    print('✅ STEP 1 完成')

    print('STEP 2/6 范围路由(3-Flash)...')
    scope = route_scope(query)
    print('✅ STEP 2 完成:', json.dumps(scope, ensure_ascii=False))

    print('STEP 3/6 本地检索(OpenViking)...')
    local_txt, coverage, meta = local_search(client, query, scope)
    print(f"✅ STEP 3 完成: coverage={coverage:.2f}, kw_cov={meta['kw_cov']:.2f}, domain_hit={meta['domain_hit']}, target_terms={meta['target_terms']}, uris={meta.get('uris',[])}")

    external_txt = ''
    if coverage < 0.65:
        print('STEP 4/6 覆盖不足，触发外部搜索(Grok)...')
        external_txt = external_search(query, scope)
        print('✅ STEP 4 完成: 外部结果长度', len(external_txt))

        print('STEP 5/6 审核并尝试入库...')
        j = judge_and_pack(query, external_txt)
        print('审核结果:', json.dumps({k:j.get(k) for k in ['pass','reason','trust','tags']}, ensure_ascii=False))
        if j.get('pass') and j.get('markdown'):
            ing = ingest_markdown(client, 'curated', j['markdown'])
            print('✅ 已入库:', ing.get('root_uri',''))
        else:
            print('⚠️ 未入库')
    else:
        print('STEP 4/6 跳过外部搜索（本地覆盖足够）')

    print('STEP 6/6 生成回答...')
    ans = answer(query, local_txt, external_txt)
    print('\n===== FINAL ANSWER =====\n')
    print(ans)


if __name__ == '__main__':
    import sys
    q = ' '.join(sys.argv[1:]).strip() or 'grok2api 自动注册需要哪些前置配置和常见失败原因？'
    run(q)
