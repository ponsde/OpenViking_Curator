"""OpenViking Curator — OV 的外搜补充层 + 治理层。

边界：
- OV 负责检索 / 分层加载 / session 记忆提取
- Curator 只做治理层：覆盖率判断、外搜补充、审核入库、冲突检测
- Curator 不生成回答（调用方自己用 LLM 组装上下文）
"""

# Re-export public API (v2 only)
from .config import (
    env, validate_config, chat, log,
    OPENVIKING_CONFIG_FILE, DATA_PATH, CURATED_DIR,
    OAI_BASE, OAI_KEY, ROUTER_MODELS, JUDGE_MODEL, JUDGE_MODELS,
    GROK_BASE, GROK_KEY, GROK_MODEL,
    _GENERIC_TERMS, FAST_ROUTE,
)
from .router import route_scope, _rule_based_scope
from .feedback import (
    load_feedback, uri_feedback_score, uri_trust_score, uri_freshness_score,
    build_feedback_priority_uris,
)
from .search import external_search, cross_validate
from .review import judge_and_pack, ingest_markdown_v2, detect_conflict
from .dedup import incremental_dedup
# v2 core
from .session_manager import OVClient, SessionManager
from .retrieval_v2 import ov_retrieve, load_context, assess_coverage
from .pipeline_v2 import run

__all__ = [
    # v2 pipeline
    "run", "ov_retrieve", "load_context", "assess_coverage",
    "OVClient", "SessionManager",
    # routing + search
    "route_scope", "external_search", "cross_validate",
    # review (governance)
    "judge_and_pack", "ingest_markdown_v2", "detect_conflict",
    # feedback
    "uri_feedback_score", "uri_trust_score", "uri_freshness_score",
    "build_feedback_priority_uris", "load_feedback",
    # config
    "chat", "env", "log", "validate_config",
]
