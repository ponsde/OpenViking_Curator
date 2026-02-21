"""In-memory backend — implements KnowledgeBackend for testing.

No external dependencies. Stores everything in plain Python dicts.
Supports sessions so that tests exercising SessionManager work without OV.
"""

from __future__ import annotations

import time
import uuid
from difflib import SequenceMatcher
from typing import Optional

from .backend import KnowledgeBackend, SearchResult, SearchResponse


class InMemoryBackend(KnowledgeBackend):
    """Pure in-memory knowledge backend for unit / integration tests.

    Features:
        - Substring + SequenceMatcher similarity search (no vectors).
        - ``ingest`` / ``read`` / ``abstract`` / ``overview`` / ``delete``.
        - Full session tracking (``create_session`` … ``session_commit``).
        - Deterministic — no randomness, no threads, no I/O.

    Example::

        backend = InMemoryBackend()
        uri = backend.ingest("Docker deployment guide", title="docker")
        resp = backend.find("docker")
        assert resp.total >= 1
    """

    def __init__(self):
        # uri → {"content": str, "title": str, "metadata": dict, "ts": float}
        self._store: dict[str, dict] = {}
        # session_id → {"messages": [(role, text)], "used": [uri], "committed": bool}
        self._sessions: dict[str, dict] = {}
        self._indexed = True  # toggle for wait_indexed tests

    # ── Required ──

    def health(self) -> bool:
        """Always healthy."""
        return True

    def find(self, query: str, limit: int = 10) -> SearchResponse:
        """Substring + similarity search across stored content.

        Args:
            query: Search string.
            limit: Max results.

        Returns:
            :class:`SearchResponse` ranked by similarity score.
        """
        results: list[SearchResult] = []
        ql = query.lower()
        for uri, rec in self._store.items():
            content = rec["content"]
            cl = content.lower()
            # Simple scoring: substring match → 0.8 base, else SequenceMatcher
            if ql in cl:
                score = 0.8
            else:
                score = SequenceMatcher(None, ql, cl[:500]).ratio()
            if score < 0.1:
                continue
            results.append(SearchResult(
                uri=uri,
                abstract=content[:100],
                overview=content[:500] if len(content) > 100 else None,
                score=round(score, 3),
                context_type="resource",
                match_reason="substring" if ql in cl else "similarity",
                metadata=rec.get("metadata", {}),
            ))
        results.sort(key=lambda r: r.score, reverse=True)
        results = results[:limit]
        return SearchResponse(results=results, total=len(results))

    def search(self, query: str, limit: int = 10,
               session_id: str = None) -> SearchResponse:
        """Delegates to :meth:`find` (no LLM analysis in memory backend).

        Args:
            query: Search string.
            limit: Max results.
            session_id: Ignored.

        Returns:
            Same as :meth:`find`.
        """
        return self.find(query, limit=limit)

    def abstract(self, uri: str) -> str:
        """First 100 chars of stored content.

        Args:
            uri: Resource identifier.

        Returns:
            Truncated content or empty string if not found.
        """
        rec = self._store.get(uri)
        return rec["content"][:100] if rec else ""

    def overview(self, uri: str) -> str:
        """First 500 chars of stored content.

        Args:
            uri: Resource identifier.

        Returns:
            Truncated content or empty string if not found.
        """
        rec = self._store.get(uri)
        return rec["content"][:500] if rec else ""

    def read(self, uri: str) -> str:
        """Full stored content.

        Args:
            uri: Resource identifier.

        Returns:
            Full content string or empty if not found.
        """
        rec = self._store.get(uri)
        return rec["content"] if rec else ""

    def ingest(self, content: str, title: str = "",
               metadata: dict = None) -> str:
        """Store content in memory and return a ``mem://`` URI.

        Args:
            content: Content to store.
            title: Human-readable title.
            metadata: Optional metadata dict.

        Returns:
            URI like ``mem://<title>`` or ``mem://<uuid>`` if no title.
        """
        safe = title.replace(" ", "_").replace("/", "_") if title else str(uuid.uuid4())[:8]
        uri = f"mem://{safe}"
        # Handle duplicate URIs by appending suffix
        if uri in self._store:
            uri = f"{uri}_{int(time.time())}"
        self._store[uri] = {
            "content": content,
            "title": title,
            "metadata": metadata or {},
            "ts": time.time(),
        }
        return uri

    # ── Optional ──

    def wait_indexed(self, timeout: int = 30):
        """No-op (indexing is immediate in memory).

        Args:
            timeout: Ignored.
        """
        pass

    def delete(self, uri: str) -> bool:
        """Remove a resource by URI.

        Args:
            uri: Resource identifier.

        Returns:
            ``True`` if found and deleted, ``False`` otherwise.
        """
        if uri in self._store:
            del self._store[uri]
            return True
        return False

    def list_resources(self, prefix: str = "") -> list[str]:
        """List all URIs, optionally filtered by prefix.

        Args:
            prefix: URI prefix to filter by (empty = all).

        Returns:
            Sorted list of URI strings.
        """
        uris = [u for u in self._store if u.startswith(prefix)] if prefix else list(self._store)
        return sorted(uris)

    # ── Session tracking ──

    def create_session(self) -> str:
        """Create a new in-memory session.

        Returns:
            Session ID string.
        """
        sid = f"memsess-{uuid.uuid4().hex[:12]}"
        self._sessions[sid] = {"messages": [], "used": [], "committed": False}
        return sid

    def session_add_message(self, session_id: str, role: str, text: str):
        """Record a message in the session.

        Args:
            session_id: Session identifier.
            role: ``"user"`` or ``"assistant"``.
            text: Message content.
        """
        sess = self._sessions.get(session_id)
        if sess is not None:
            sess["messages"].append((role, text))

    def session_used(self, session_id: str, uris: list[str]):
        """Mark URIs as used in this session.

        Args:
            session_id: Session identifier.
            uris: List of resource URIs.
        """
        sess = self._sessions.get(session_id)
        if sess is not None:
            sess["used"].extend(uris)

    def session_commit(self, session_id: str) -> dict:
        """Commit session (marks as committed, returns summary).

        Args:
            session_id: Session identifier.

        Returns:
            Dict with ``memories_extracted``, ``active_count_updated``, ``archived``.
        """
        sess = self._sessions.get(session_id)
        if sess is None:
            return {}
        sess["committed"] = True
        return {
            "memories_extracted": 0,
            "active_count_updated": len(sess["used"]),
            "archived": False,
        }

    # ── Metadata ──

    @property
    def name(self) -> str:
        return "InMemory"

    @property
    def supports_sessions(self) -> bool:
        return True

    @property
    def supports_llm_search(self) -> bool:
        return False
