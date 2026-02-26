"""OpenViking Curator — Knowledge governance layer.

Architecture:
- KnowledgeBackend: abstract interface for any knowledge store
- OpenVikingBackend: default implementation (embedded + HTTP mode)
- InMemoryBackend: pure-Python backend for testing (no external deps)
- Pipeline: query → retrieve → assess → search → review → ingest → return context
- Curator does NOT generate answers — returns structured data for the caller's LLM

Boundaries:
- Backend handles: retrieval, storage, indexing, session tracking
- Curator handles: coverage assessment, external search, review, ingest,
                   conflict detection + resolution, freshness scoring, dedup scanning
"""

from ._version import __version__

# Re-export public API
from .backend import KnowledgeBackend, SearchResponse, SearchResult
from .backend_memory import InMemoryBackend
from .backend_ov import OpenVikingBackend
from .config import (
    CURATED_DIR,
    DATA_PATH,
    FAST_ROUTE,
    GROK_BASE,
    GROK_KEY,
    GROK_MODEL,
    JUDGE_MODEL,
    JUDGE_MODELS,
    OAI_BASE,
    OAI_KEY,
    OPENVIKING_CONFIG_FILE,
    ROUTER_MODELS,
    chat,
    env,
    log,
    validate_config,
)
from .decision_report import format_report, format_report_short
from .dedup import scan_duplicates
from .domain_filter import (
    build_domain_prompt_hint,
    domain_matches,
    extract_domain,
    filter_results_by_domain,
    filter_text_by_domain,
)
from .freshness import uri_freshness_score
from .pipeline_v2 import run
from .retrieval_v2 import assess_coverage, load_context, ov_retrieve
from .review import (
    JudgeResult,
    detect_conflict,
    ingest_markdown_v2,
    judge_and_ingest,
    judge_and_pack,
)
from .router import route_scope
from .search import cross_validate, external_search

# Deprecated: use OpenVikingBackend instead; OVClient/SessionManager will be
# removed in a future release once all code paths use KnowledgeBackend.
from .session_manager import OVClient, SessionManager

__all__ = [
    # Abstract backend interface
    "KnowledgeBackend",
    "SearchResult",
    "SearchResponse",
    # OpenViking backend (default)
    "OpenVikingBackend",
    # In-memory backend (testing)
    "InMemoryBackend",
    # v2 pipeline
    "run",
    "ov_retrieve",
    "load_context",
    "assess_coverage",
    "OVClient",
    "SessionManager",
    # routing + search + review (governance)
    "route_scope",
    "external_search",
    "cross_validate",
    "judge_and_pack",
    "judge_and_ingest",
    "ingest_markdown_v2",
    "detect_conflict",
    "JudgeResult",
    # supplementary (OV doesn't have these)
    "uri_freshness_score",
    "scan_duplicates",
    "extract_domain",
    "domain_matches",
    "filter_results_by_domain",
    "filter_text_by_domain",
    "build_domain_prompt_hint",
    "format_report",
    "format_report_short",
    # config
    "chat",
    "env",
    "log",
    "validate_config",
    # version
    "__version__",
]
