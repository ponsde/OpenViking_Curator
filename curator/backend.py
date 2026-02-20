"""Storage backend interface for Curator.

Curator needs a knowledge store that can:
  - search/find by semantic query
  - read content at different granularity (abstract → overview → full)
  - ingest new content
  - track usage (optional)

OpenViking is the default backend, but any system implementing
KnowledgeBackend can be used (Milvus, Qdrant, Chroma, pgvector, etc.)
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SearchResult:
    """A single search result from the knowledge backend."""
    uri: str                          # unique identifier for the resource
    abstract: str = ""                # short summary (~100 tokens)
    overview: Optional[str] = None    # medium summary (~2k tokens)
    score: float = 0.0                # relevance score (0~1)
    context_type: str = ""            # e.g. "resource", "memory", "skill"
    match_reason: str = ""            # why this matched
    category: str = ""                # topic category
    relations: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class SearchResponse:
    """Aggregated search results."""
    results: list[SearchResult]       # all matching items
    total: int = 0
    query_plan: Optional[dict] = None # backend's query analysis (if any)


class KnowledgeBackend(ABC):
    """Abstract interface for knowledge storage backends.

    Implement this to plug Curator into any knowledge store.
    """

    @abstractmethod
    def health(self) -> bool:
        """Check if the backend is reachable and healthy."""
        ...

    @abstractmethod
    def find(self, query: str, limit: int = 10) -> SearchResponse:
        """Semantic vector search (fast, no LLM)."""
        ...

    @abstractmethod
    def search(self, query: str, limit: int = 10, session_id: str = None) -> SearchResponse:
        """Full search with optional LLM intent analysis.

        Backends without LLM search can delegate to find().
        """
        ...

    @abstractmethod
    def abstract(self, uri: str) -> str:
        """Get short summary (~100 tokens) of a resource."""
        ...

    @abstractmethod
    def overview(self, uri: str) -> str:
        """Get medium summary (~2k tokens) of a resource."""
        ...

    @abstractmethod
    def read(self, uri: str) -> str:
        """Get full content of a resource."""
        ...

    @abstractmethod
    def ingest(self, content: str, title: str = "", metadata: dict = None) -> str:
        """Store new content. Returns the URI/ID of the stored resource."""
        ...

    def wait_indexed(self, timeout: int = 30):
        """Wait until recently ingested content is indexed. Optional."""
        pass

    def delete(self, uri: str) -> bool:
        """Delete a resource by URI. Optional."""
        return False

    def list_resources(self, prefix: str = "") -> list[str]:
        """List resource URIs. Optional."""
        return []

    # ── Session tracking (optional) ──

    def create_session(self) -> str:
        """Create a tracking session. Returns session_id."""
        return ""

    def session_add_message(self, session_id: str, role: str, text: str):
        """Record a message in the session."""
        pass

    def session_used(self, session_id: str, uris: list[str]):
        """Mark resources as actually used in this session."""
        pass

    def session_commit(self, session_id: str) -> dict:
        """Commit session: extract memories, update usage counts."""
        return {}

    # ── Metadata ──

    @property
    def name(self) -> str:
        """Human-readable backend name."""
        return self.__class__.__name__

    @property
    def supports_sessions(self) -> bool:
        """Whether this backend supports session tracking."""
        return False

    @property
    def supports_llm_search(self) -> bool:
        """Whether search() uses LLM intent analysis (vs plain vector search)."""
        return False
