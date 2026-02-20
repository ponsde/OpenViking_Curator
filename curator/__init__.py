"""OpenViking Curator — OV 的知识治理层。

边界：
- OV 负责检索 / 分层加载 / session 记忆提取 / memory 去重
- Curator 做治理层：覆盖率判断、外搜补充、审核入库、冲突检测
- Curator 补充 OV 没有的：时效评分（语义层）、resources 相似度检测
- Curator 不生成回答（调用方自己用 LLM 组装上下文）
- Curator 不重新排序 OV 检索结果（信任 OV 的 score）
"""

# Re-export public API (v2 only)
from .config import (
    env, validate_config, chat, log,
    OPENVIKING_CONFIG_FILE, DATA_PATH, CURATED_DIR,
    OAI_BASE, OAI_KEY, ROUTER_MODELS, JUDGE_MODEL, JUDGE_MODELS,
    GROK_BASE, GROK_KEY, GROK_MODEL,
    _GENERIC_TERMS, FAST_ROUTE,
)
from .search import external_search, cross_validate
from .review import judge_and_pack, judge_and_ingest, ingest_markdown_v2, detect_conflict
from .router import route_scope
from .freshness import uri_freshness_score
from .dedup import scan_duplicates
# v2 core
from .session_manager import OVClient, SessionManager
from .retrieval_v2 import ov_retrieve, load_context, assess_coverage
from .pipeline_v2 import run

__all__ = [
    # v2 pipeline
    "run", "ov_retrieve", "load_context", "assess_coverage",
    "OVClient", "SessionManager",
    # routing + search + review (governance)
    "route_scope",
    "external_search", "cross_validate",
    "judge_and_pack", "judge_and_ingest", "ingest_markdown_v2", "detect_conflict",
    # supplementary (OV doesn't have these)
    "uri_freshness_score", "scan_duplicates",
    # config
    "chat", "env", "log", "validate_config",
]
