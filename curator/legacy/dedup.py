"""Dedup: incremental deduplication during each pipeline run."""

import json
import os
import re
import time
from pathlib import Path
from difflib import SequenceMatcher

from .config import log, chat, OAI_BASE, OAI_KEY, ANSWER_MODELS

DEDUP_LOG_FILE = os.getenv("CURATOR_DEDUP_LOG", "dedup_log.json")
SIMILARITY_THRESHOLD = 0.55  # 文本相似度阈值，超过则认为可能重复


def _load_dedup_log() -> dict:
    try:
        if os.path.exists(DEDUP_LOG_FILE):
            return json.loads(Path(DEDUP_LOG_FILE).read_text())
    except Exception:
        pass
    return {"checked_pairs": [], "merged": [], "last_run": None}


def _save_dedup_log(state: dict):
    state["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # 只保留最近 500 对检查记录，防止无限膨胀
    state["checked_pairs"] = state["checked_pairs"][-500:]
    state["merged"] = state["merged"][-100:]
    Path(DEDUP_LOG_FILE).write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _text_similarity(a: str, b: str) -> float:
    """快速文本相似度（SequenceMatcher），不调 API。"""
    if not a or not b:
        return 0.0
    # 截断长文本，只比前 1000 字
    a = a[:1000].lower().strip()
    b = b[:1000].lower().strip()
    return SequenceMatcher(None, a, b).ratio()


def _pair_key(uri_a: str, uri_b: str) -> str:
    """生成有序的 pair key，避免 (a,b) 和 (b,a) 重复检查。"""
    return "|".join(sorted([uri_a, uri_b]))


_MERGE_PROMPT = """你是知识库去重助手。下面两篇文档内容高度相似，请合并为一篇。

要求：
1. 保留双方所有有价值的独特信息，不丢任何细节
2. 去除纯重复的部分
3. 如果两篇角度不同，都保留
4. 输出格式为 markdown，带标题
5. 如果两篇完全一样（没有任何独特信息），直接输出较完整的那篇

文档 A（URI: {uri_a}）:
{content_a}

文档 B（URI: {uri_b}）:
{content_b}

请输出合并后的文档（纯 markdown，不要解释）："""


def _llm_merge(uri_a: str, content_a: str, uri_b: str, content_b: str) -> str | None:
    """用 LLM 合并两篇相似文档。"""
    prompt = _MERGE_PROMPT.format(
        uri_a=uri_a, content_a=content_a[:2000],
        uri_b=uri_b, content_b=content_b[:2000],
    )
    for model in ANSWER_MODELS:
        try:
            result = chat(OAI_BASE, OAI_KEY, model, [
                {"role": "user", "content": prompt},
            ], timeout=30)
            return result
        except Exception as e:
            log.debug("dedup merge model %s failed: %s", model, e)
            continue
    return None


def incremental_dedup(client, uris: list[str], max_checks: int = 3) -> dict:
    """
    渐进式去重：从本次检索命中的 URI 中检查相似对。
    每次最多检查 max_checks 对，LLM 合并后替换入库。
    
    Returns: {"checked": int, "merged": int, "details": [...]}
    """
    state = _load_dedup_log()
    checked_set = set(state["checked_pairs"])
    
    result = {"checked": 0, "merged": 0, "details": []}
    
    # 过滤有效 URI（只处理 curated 的，不动原始数据）
    valid_uris = [u for u in uris if "curated" in u.lower() and u.startswith("viking://")]
    if len(valid_uris) < 2:
        return result
    
    # 读取内容
    uri_contents = {}
    for u in valid_uris[:10]:  # 最多看 10 个
        try:
            content = str(client.read(u))
            if content and len(content) > 50:
                uri_contents[u] = content
        except Exception:
            continue
    
    if len(uri_contents) < 2:
        return result
    
    # 两两比较（只检查未检查过的对）
    checks_done = 0
    uri_list = list(uri_contents.keys())
    
    for i in range(len(uri_list)):
        if checks_done >= max_checks:
            break
        for j in range(i + 1, len(uri_list)):
            if checks_done >= max_checks:
                break
                
            uri_a, uri_b = uri_list[i], uri_list[j]
            pk = _pair_key(uri_a, uri_b)
            
            if pk in checked_set:
                continue
            
            # 快速文本相似度检查
            sim = _text_similarity(uri_contents[uri_a], uri_contents[uri_b])
            checked_set.add(pk)
            state["checked_pairs"].append(pk)
            checks_done += 1
            result["checked"] += 1
            
            if sim < SIMILARITY_THRESHOLD:
                continue
            
            log.info("dedup: 发现相似对 (%.2f): %s vs %s", sim, uri_a, uri_b)
            
            # LLM 合并
            merged_content = _llm_merge(
                uri_a, uri_contents[uri_a],
                uri_b, uri_contents[uri_b],
            )
            
            if not merged_content:
                log.warning("dedup: LLM 合并失败，跳过")
                continue
            
            # 保留较新的 URI，用合并内容替换
            # 写入合并后的文件，重新入库
            curated_dir = os.getenv("CURATOR_CURATED_DIR", "curated")
            os.makedirs(curated_dir, exist_ok=True)
            
            ts = int(time.time())
            merge_path = os.path.join(curated_dir, f"merged_{ts}.md")
            Path(merge_path).write_text(merged_content, encoding="utf-8")
            
            try:
                # 入库合并后的文档
                client.add_resource(path=merge_path, target="curated",
                                   reason=f"merged from {uri_a} and {uri_b}", wait=False)
                # 删除旧的两条
                for old_uri in [uri_a, uri_b]:
                    try:
                        client.rm(old_uri)
                        log.info("dedup: 已删除 %s", old_uri)
                    except Exception as e:
                        log.warning("dedup: 删除 %s 失败: %s", old_uri, e)
                
                result["merged"] += 1
                state["merged"].append({
                    "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "uri_a": uri_a,
                    "uri_b": uri_b,
                    "similarity": round(sim, 3),
                    "merged_path": merge_path,
                })
                result["details"].append(f"合并: {uri_a} + {uri_b} (相似度 {sim:.2f})")
                
            except Exception as e:
                log.warning("dedup: 入库合并文档失败: %s", e)
    
    _save_dedup_log(state)
    return result
