"""OpenViking Curator — Knowledge governance layer.

Architecture:
- KnowledgeBackend: abstract interface for any knowledge store
- OpenVikingBackend: default implementation (embedded + HTTP mode)
- Pipeline: query → retrieve → assess → search → review → ingest → return context
- Curator does NOT generate answers — returns structured data for the caller's LLM

Boundaries:
- Backend handles: retrieval, storage, indexing, session tracking
- Curator handles: coverage assessment, external search, review, ingest,
                   conflict detection + resolution, freshness scoring, dedup scanning
"""

# Re-export public API
from .backend import KnowledgeBackend, SearchResult, SearchResponse
from .backend_ov import OpenVikingBackend
from .config import (
    env, validate_config, chat, log,
    OPENVIKING_CONFIG_FILE, DATA_PATH, CURATED_DIR,
    OAI_BASE, OAI_KEY, ROUTER_MODELS, JUDGE_MODEL, JUDGE_MODELS,
    GROK_BASE, GROK_KEY, GROK_MODEL,
    FAST_ROUTE,
)
from .search import external_search, cross_validate
from .review import judge_and_pack, judge_and_ingest, ingest_markdown_v2, detect_conflict
from .router import route_scope
from .freshness import uri_freshness_score
from .dedup import scan_duplicates
from .session_manager import OVClient, SessionManager
from .retrieval_v2 import ov_retrieve, load_context, assess_coverage
from .pipeline_v2 import run

__all__ = [
    # Abstract backend interface
    "KnowledgeBackend", "SearchResult", "SearchResponse",
    # OpenViking backend (default)
    "OpenVikingBackend",
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
