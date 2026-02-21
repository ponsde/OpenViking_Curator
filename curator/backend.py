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
    """A single search result from the knowledge backend.

    Attributes:
        uri: Unique identifier for the resource (e.g. ``viking://resources/...``).
        abstract: Short summary (~100 tokens).
        overview: Medium summary (~2k tokens), ``None`` if not loaded.
        score: Relevance score normalised to 0–1.
        context_type: E.g. ``"resource"``, ``"memory"``, ``"skill"``.
        match_reason: Human-readable explanation of why this matched.
        category: Topic category assigned by the backend.
        relations: List of related URIs / relation objects.
        metadata: Arbitrary extra metadata dict.
    """

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
    """Aggregated search results.

    Attributes:
        results: All matching items, sorted by relevance.
        total: Total number of results (may exceed ``len(results)``).
        query_plan: Backend's query analysis / intent decomposition, if any.
    """

    results: list[SearchResult]       # all matching items
    total: int = 0
    query_plan: Optional[dict] = None # backend's query analysis (if any)


class KnowledgeBackend(ABC):
    """Abstract interface for knowledge storage backends.

    Implement this to plug Curator into any knowledge store.

    Methods are divided into three tiers:

    1. **Required** (must implement): ``health``, ``find``, ``search``,
       ``abstract``, ``overview``, ``read``, ``ingest``.
    2. **Optional with sensible defaults**: ``wait_indexed``, ``delete``,
       ``list_resources``.
    3. **Session tracking** (optional, default no-op): ``create_session``,
       ``session_add_message``, ``session_used``, ``session_commit``.
    """

    # ── Required: core CRUD + retrieval ──

    @abstractmethod
    def health(self) -> bool:
        """Check if the backend is reachable and healthy.

        Returns:
            ``True`` if the backend is ready to serve requests.
        """
        ...

    @abstractmethod
    def find(self, query: str, limit: int = 10) -> SearchResponse:
        """Semantic vector search (fast, no LLM).

        Args:
            query: Natural-language search query.
            limit: Maximum number of results to return.

        Returns:
            A :class:`SearchResponse` with matching items.
        """
        ...

    @abstractmethod
    def search(self, query: str, limit: int = 10,
               session_id: str = None) -> SearchResponse:
        """Full search with optional LLM intent analysis.

        Backends without LLM search can delegate to :meth:`find`.

        Args:
            query: Natural-language search query.
            limit: Maximum number of results to return.
            session_id: Optional session id for context-aware search.

        Returns:
            A :class:`SearchResponse` with matching items.
        """
        ...

    @abstractmethod
    def abstract(self, uri: str) -> str:
        """Get short summary (~100 tokens) of a resource.

        Args:
            uri: Resource identifier.

        Returns:
            Short plain-text summary.
        """
        ...

    @abstractmethod
    def overview(self, uri: str) -> str:
        """Get medium summary (~2k tokens) of a resource.

        Args:
            uri: Resource identifier.

        Returns:
            Medium-length plain-text summary.
        """
        ...

    @abstractmethod
    def read(self, uri: str) -> str:
        """Get full content of a resource.

        Args:
            uri: Resource identifier.

        Returns:
            Full content as string.
        """
        ...

    @abstractmethod
    def ingest(self, content: str, title: str = "",
               metadata: dict = None) -> str:
        """Store new content. Returns the URI/ID of the stored resource.

        Args:
            content: Raw content to ingest (typically Markdown).
            title: Human-readable title for the resource.
            metadata: Optional metadata dict (tags, freshness, etc.).

        Returns:
            URI or ID string of the newly stored resource.
        """
        ...

    # ── Optional with sensible defaults ──

    def wait_indexed(self, timeout: int = 30):
        """Wait until recently ingested content is indexed.

        Backends with synchronous indexing can leave this as a no-op.

        Args:
            timeout: Maximum seconds to wait.
        """
        pass

    def delete(self, uri: str) -> bool:
        """Delete a resource by URI.

        Args:
            uri: Resource identifier to delete.

        Returns:
            ``True`` if the resource was successfully deleted.
        """
        return False

    def list_resources(self, prefix: str = "") -> list[str]:
        """List resource URIs, optionally filtered by prefix.

        Args:
            prefix: URI prefix filter (empty = list all).

        Returns:
            List of URI strings.
        """
        return []

    # ── Session tracking (optional) ──

    def create_session(self) -> str:
        """Create a tracking session.

        Returns:
            Session ID string (empty if sessions are not supported).
        """
        return ""

    def session_add_message(self, session_id: str, role: str, text: str):
        """Record a message in the session.

        Args:
            session_id: Session identifier from :meth:`create_session`.
            role: ``"user"`` or ``"assistant"``.
            text: Message content.
        """
        pass

    def session_used(self, session_id: str, uris: list[str]):
        """Mark resources as actually used in this session.

        Args:
            session_id: Session identifier.
            uris: List of resource URIs that were used.
        """
        pass

    def session_commit(self, session_id: str) -> dict:
        """Commit session: extract memories, update usage counts.

        Args:
            session_id: Session identifier.

        Returns:
            Dict with commit results (e.g. ``memories_extracted``, ``archived``).
        """
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
        """Whether :meth:`search` uses LLM intent analysis (vs plain vector)."""
        return False
