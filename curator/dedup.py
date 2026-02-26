"""Dedup: resources 去重（只报告模式）。

OV 的 memory_deduplicator 只管 memory 去重，不管 resources。
这个模块补充 resources 层的相似度检测。

注意：只报告疑似重复，不自动删除/合并。

All knowledge-store operations go through KnowledgeBackend (or duck-typed compatible).

## 两层去重策略

Layer 1 — URL hash（O(1) per pair）：
    从文本中 regex 抽取所有 http(s) URL，取 md5 哈希集合。
    两篇文章 URL 哈希有交集 → 直接判定重复，不再做文本相似度。
    这能精确命中「同一来源被入库两次」的场景。

Layer 2 — Jaccard 词集合（无外部依赖）：
    对词集合计算 |intersection| / |union|（Jaccard index）。
    相比旧版 SequenceMatcher（字符级），Jaccard 对词序不敏感、
    更能反映语义重叠，且时间复杂度从 O(n²) 降到 O(vocab)。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .config import env, log

if TYPE_CHECKING:
    from .backend import KnowledgeBackend

DEDUP_LOG_FILE = os.getenv(
    "CURATOR_DEDUP_LOG",
    os.path.join(
        os.environ.get(
            "CURATOR_DATA_PATH", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        ),
        "dedup_log.json",
    ),
)
try:
    SIMILARITY_THRESHOLD = max(0.0, min(1.0, float(env("CURATOR_DEDUP_SIMILARITY", "0.55"))))
except (ValueError, TypeError):
    log.warning("invalid CURATOR_DEDUP_SIMILARITY, using default 0.55")
    SIMILARITY_THRESHOLD = 0.55

try:
    MAX_SCAN_ITEMS = max(1, int(env("CURATOR_DEDUP_MAX_ITEMS", "10")))
except (ValueError, TypeError):
    log.warning("invalid CURATOR_DEDUP_MAX_ITEMS, using default 10")
    MAX_SCAN_ITEMS = 10

_URL_RE = re.compile(r"https?://[^\s)\]>\"']{8,}")


# ── Layer 1: URL hash ────────────────────────────────────────────────────────


def _url_hashes(text: str) -> frozenset:
    """从文本中提取所有 http(s) URL，返回其 md5 哈希集合。

    使用哈希而非原始 URL，避免查询时泄漏完整 URL，同时保持 O(1) 交集判断。
    """
    urls = _URL_RE.findall(text or "")
    return frozenset(hashlib.md5(u.strip().lower().encode("utf-8")).hexdigest() for u in urls)


def _url_overlap(hashes_a: frozenset, hashes_b: frozenset) -> bool:
    """两个文档的 URL 哈希集合有交集 → 视为来源重叠的重复。

    只在双方各至少有 1 个 URL 时才判断，避免把「没有 URL 的文档」都误判为非重复。
    """
    if not hashes_a or not hashes_b:
        return False
    return bool(hashes_a & hashes_b)


# ── Layer 2: Jaccard 词相似度 ─────────────────────────────────────────────────


def _tokenize(text: str) -> frozenset:
    """简单分词：小写化 + 按非字母数字分割，过滤短词。

    CJK 字符（\\u4e00-\\u9fff）保留单字，因为中文里单个汉字也有区分性
    （技术文档里「库、图、型、表」等单字有意义）。
    其他字符（拉丁、数字等）过滤掉 len < 2 的词（单字母无意义）。
    """
    tokens = re.split(r"[^a-z0-9\u4e00-\u9fff]+", text.lower())
    result = set()
    for w in tokens:
        if not w:
            continue
        # CJK 单字保留，其他词过滤掉单字符
        if len(w) == 1 and not ("\u4e00" <= w <= "\u9fff"):
            continue
        result.add(w)
    return frozenset(result)


def _jaccard_similarity(a: str, b: str) -> float:
    """词集合 Jaccard 相似度：|A∩B| / |A∪B|。

    相比 SequenceMatcher：
    - 对词序不敏感（重组段落仍能检出）
    - 时间复杂度 O(vocab)，比字符级 O(n²) 快
    - 无外部依赖
    """
    if not a or not b:
        return 0.0
    tokens_a = _tokenize(a[:2000])
    tokens_b = _tokenize(b[:2000])
    if not tokens_a or not tokens_b:
        return 0.0
    inter = len(tokens_a & tokens_b)
    union = len(tokens_a | tokens_b)
    return inter / union if union else 0.0


# ── 去重日志 I/O ─────────────────────────────────────────────────────────────


def _load_dedup_log() -> dict:
    try:
        if os.path.exists(DEDUP_LOG_FILE):
            return json.loads(Path(DEDUP_LOG_FILE).read_text())
    except Exception as e:
        log.debug("failed to load dedup log from %s: %s", DEDUP_LOG_FILE, e)
    return {"checked_pairs": [], "reports": [], "last_run": None}


def _save_dedup_log(state: dict):
    from .file_lock import locked_write

    state["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["checked_pairs"] = state["checked_pairs"][-500:]
    state["reports"] = state["reports"][-100:]

    locked_write(DEDUP_LOG_FILE, json.dumps(state, ensure_ascii=False, indent=2))


def _pair_key(uri_a: str, uri_b: str) -> str:
    return "|".join(sorted([uri_a, uri_b]))


# ── 公开接口 ──────────────────────────────────────────────────────────────────


def scan_duplicates(backend, uris: list[str], max_checks: int = 0) -> dict:
    """扫描 resources 中的疑似重复（只报告，不删除）。

    **去重流程（两层）：**

    1. Layer 1 — URL hash：两篇文章共享任一来源 URL → 直接标记重复，
       ``method="url_hash"``，不再做文本比较。

    2. Layer 2 — Jaccard 词相似度：无 URL 交集时比较词集合，
       超过 ``SIMILARITY_THRESHOLD`` (默认 0.55) 则报告重复，
       ``method="jaccard"``。

    Args:
        backend: A :class:`KnowledgeBackend` instance (or any object with a
                 ``read(uri)`` method).
        uris: List of resource URIs to compare.
        max_checks: Maximum number of pair-wise comparisons.
                    0（默认）= 自适应：min(50, len(uris) * 3)，
                    知识库越大，相对扫描比例越合理。

    Returns:
        Dict with ``checked`` (int) and ``duplicates`` (list of dicts with
        ``uri_a``, ``uri_b``, ``similarity``, ``method``).
    """
    state = _load_dedup_log()
    checked_set = set(state["checked_pairs"])

    result = {"checked": 0, "duplicates": []}

    valid_uris = [u for u in uris if u.startswith("viking://") or u.startswith("mem://")]
    if len(valid_uris) < 2:
        return result

    # 自适应扫描上限：0 = 自适应模式
    if max_checks <= 0:
        max_checks = min(50, len(valid_uris) * 3)

    # 读取内容
    uri_contents: dict[str, str] = {}
    for u in valid_uris[:MAX_SCAN_ITEMS]:
        try:
            content = str(backend.read(u))
            if content and len(content) > 50:
                uri_contents[u] = content
        except Exception as e:
            log.debug("failed to read content for URI %s: %s", u, e)
            continue

    if len(uri_contents) < 2:
        return result

    # 预计算 URL hash 集合（Layer 1）
    uri_url_hashes: dict[str, frozenset] = {u: _url_hashes(text) for u, text in uri_contents.items()}

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

            checked_set.add(pk)
            state["checked_pairs"].append(pk)
            checks_done += 1
            result["checked"] += 1

            # Layer 1: URL hash 精确匹配
            if _url_overlap(uri_url_hashes[uri_a], uri_url_hashes[uri_b]):
                # sim=1.0 是哨兵值，表示「共享来源 URL」，不代表内容 100% 一致
                # （同一 URL 的摘要 vs 全文仍可能内容不同）
                # method="url_hash" 时 similarity 字段含义：来源重叠，非内容相似度
                sim = 1.0
                method = "url_hash"
            else:
                # Layer 2: Jaccard 词相似度
                sim = _jaccard_similarity(uri_contents[uri_a], uri_contents[uri_b])
                method = "jaccard"

            if sim >= SIMILARITY_THRESHOLD:
                log.info("dedup: 疑似重复 (%.2f, %s): %s vs %s", sim, method, uri_a, uri_b)
                dup = {
                    "uri_a": uri_a,
                    "uri_b": uri_b,
                    "similarity": round(sim, 3),
                    "method": method,
                }
                result["duplicates"].append(dup)
                state["reports"].append(
                    {
                        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        **dup,
                    }
                )

    _save_dedup_log(state)
    return result
