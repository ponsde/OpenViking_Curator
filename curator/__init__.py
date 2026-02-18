"""OpenViking Curator — Active knowledge governance for OpenViking."""

# Re-export public API so `from curator import X` still works
from .config import (
    env, validate_config, chat, log,
    OPENVIKING_CONFIG_FILE, DATA_PATH, CURATED_DIR,
    OAI_BASE, OAI_KEY, ROUTER_MODELS, JUDGE_MODEL, JUDGE_MODELS, ANSWER_MODELS,
    GROK_BASE, GROK_KEY, GROK_MODEL,
    THRESHOLD_LOW_COV, THRESHOLD_CORE_COV, THRESHOLD_LOW_TRUST,
    THRESHOLD_LOW_FRESH, THRESHOLD_CURATED_OVERLAP, THRESHOLD_CURATED_MIN_HITS,
    _GENERIC_TERMS, FAST_ROUTE,
)
from .router import route_scope, _rule_based_scope
from .feedback import (
    load_feedback, uri_feedback_score, uri_trust_score, uri_freshness_score,
    build_feedback_priority_uris,
)
from .retrieval import (
    deterministic_relevance, local_search, build_priority_context,
)
from .search import external_boost_needed, external_search, cross_validate
from .review import judge_and_pack, ingest_markdown, detect_conflict
from .answer import answer, _build_source_footer
from .pipeline import run

__all__ = [
    "run", "route_scope", "local_search", "external_search",
    "external_boost_needed", "cross_validate",
    "judge_and_pack", "ingest_markdown", "detect_conflict",
    "answer", "build_priority_context", "validate_config",
    "deterministic_relevance", "_build_source_footer",
    "uri_feedback_score", "uri_trust_score", "uri_freshness_score",
    "build_feedback_priority_uris", "load_feedback",
    "chat", "env", "log",
]
