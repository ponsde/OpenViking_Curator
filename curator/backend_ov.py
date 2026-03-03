"""OpenViking backend — implements KnowledgeBackend for OV (embedded + HTTP).

All OV-specific code (viking:// URIs, AGFS, session objects) lives here,
not in the pipeline.
"""

import asyncio
import json
import os
import re
import tempfile
import threading
import urllib.parse
import urllib.request

from .backend import KnowledgeBackend, SearchResponse, SearchResult
from .config import CURATED_DIR, log

_DEFAULT_DATA_PATH = os.environ.get(
    "OV_DATA_PATH",
    os.environ.get("CURATOR_DATA_PATH", "./data"),
)


class _HTTPClient:
    """Interact with OV HTTP serve API."""

    def __init__(self, base_url: str):
        self._base = base_url.rstrip("/")

    def _request(self, method: str, path: str, data: dict | None = None, params: dict | None = None, timeout: int = 60):
        url = f"{self._base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        if method == "GET":
            req = urllib.request.Request(url)
        else:
            req = urllib.request.Request(
                url,
                data=json.dumps(data or {}).encode(),
                headers={"Content-Type": "application/json"},
                method=method,
            )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read())
        if isinstance(resp, dict) and "result" in resp:
            if resp.get("error"):
                raise RuntimeError(f"OV API error: {resp['error']}")
            return resp["result"]
        return resp

    def health(self) -> bool:
        try:
            r = self._request("GET", "/health")
            return r.get("status") == "ok" if isinstance(r, dict) else True
        except Exception as e:
            log.debug("HTTP health check failed: %s", e)
            return False

    def find(self, query: str, limit: int = 10, target_uri: str = "") -> dict:
        data = {"query": query, "limit": limit}
        if target_uri:
            data["target_uri"] = target_uri
        result = self._request("POST", "/api/v1/search/find", data)
        return self._normalize_find(result)

    def search(self, query: str, session_id: str | None = None, limit: int = 10) -> dict:
        data = {"query": query, "limit": limit}
        if session_id:
            data["session_id"] = session_id
        result = self._request("POST", "/api/v1/search/search", data, timeout=120)
        return self._normalize_find(result)

    @staticmethod
    def _normalize_find(result: dict) -> dict:
        if not isinstance(result, dict):
            return {"memories": [], "resources": [], "skills": [], "total": 0}
        memories = result.get("memories", [])
        resources = result.get("resources", [])
        skills = result.get("skills", [])
        return {
            "memories": memories,
            "resources": resources,
            "skills": skills,
            "query_plan": result.get("query_plan"),
            "total": len(memories) + len(resources) + len(skills),
        }

    def abstract(self, uri: str) -> str:
        r = self._request("GET", "/api/v1/content/abstract", params={"uri": uri})
        return r if isinstance(r, str) else r.get("abstract", str(r))

    def overview(self, uri: str) -> str:
        r = self._request("GET", "/api/v1/content/overview", params={"uri": uri})
        return r if isinstance(r, str) else r.get("overview", str(r))

    def read(self, uri: str) -> str:
        r = self._request("GET", "/api/v1/content/read", params={"uri": uri})
        return r if isinstance(r, str) else r.get("content", str(r))

    def add_resource(self, path: str, reason: str = "", wait: bool = False) -> dict:
        return self._request("POST", "/api/v1/resources", {"path": path, "reason": reason, "wait": wait}, timeout=120)

    def ls(self, uri: str, **kwargs) -> list:
        r = self._request("GET", "/api/v1/fs/ls", params={"uri": uri, **kwargs})
        return r if isinstance(r, list) else r.get("items", [])

    def grep(self, uri: str, pattern: str) -> dict:
        return self._request("POST", "/api/v1/search/grep", {"uri": uri, "pattern": pattern})

    def wait_processed(self, timeout: int = 30) -> dict:
        return self._request("POST", "/api/v1/system/wait", {"timeout": timeout}, timeout=timeout + 10)

    def link(self, from_uri: str, to_uris: list, reason: str = ""):
        self._request("POST", "/api/v1/relations/link", {"from_uri": from_uri, "to_uris": to_uris, "reason": reason})

    def create_session(self) -> dict:
        return self._request("POST", "/api/v1/sessions", {})

    def session_add_message(self, session_id: str, role: str, text: str):
        self._request("POST", f"/api/v1/sessions/{session_id}/messages", {"role": role, "content": text})

    def session_used(self, session_id: str, uris: list):
        # OV HTTP serve currently has no session.used() endpoint
        log.debug("HTTP mode does not support session.used(), skipping")

    def session_commit(self, session_id: str) -> dict:
        return self._request("POST", f"/api/v1/sessions/{session_id}/commit", {}, timeout=120)


_ov_loop = None
_ov_thread = None
_ov_lock = threading.Lock()


def _get_ov_loop():
    global _ov_loop, _ov_thread
    if _ov_loop is not None and not _ov_loop.is_closed():
        return _ov_loop
    with _ov_lock:
        if _ov_loop is not None and not _ov_loop.is_closed():
            return _ov_loop
        _ov_loop = asyncio.new_event_loop()
        _ov_thread = threading.Thread(target=_ov_loop.run_forever, daemon=True)
        _ov_thread.start()
    return _ov_loop


def _ov_run(coro):
    loop = _get_ov_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=120)


_async_client = None
_client_lock = threading.Lock()


def _get_async_client():
    global _async_client
    if _async_client is not None:
        return _async_client
    with _client_lock:
        if _async_client is not None:
            return _async_client
        from openviking import AsyncOpenViking

        data_path = os.environ.get("OV_DATA_PATH", _DEFAULT_DATA_PATH)
        _async_client = AsyncOpenViking(path=data_path)
        _ov_run(_async_client.initialize())
        log.info("OV embedded mode initialized: %s", data_path)
    return _async_client


class _EmbeddedClient:
    """Direct calls via AsyncOpenViking embedded mode."""

    def __init__(self):
        self._client = _get_async_client()

    @property
    def raw(self):
        return self._client

    def health(self) -> bool:
        try:
            return self._client.is_healthy()
        except Exception as e:
            log.debug("embedded client health check failed: %s", e)
            return False

    def find(self, query, limit=10, target_uri=""):
        result = _ov_run(self._client.find(query, target_uri=target_uri, limit=limit))
        return self._to_dict(result)

    def search(self, query, session_id=None, limit=10):
        result = _ov_run(self._client.search(query, session_id=session_id, limit=limit))
        return self._to_dict(result)

    @staticmethod
    def _to_dict(result):
        def _ctx(ctx):
            return {
                "uri": getattr(ctx, "uri", ""),
                "context_type": getattr(ctx, "context_type", ""),
                "abstract": getattr(ctx, "abstract", ""),
                "overview": getattr(ctx, "overview", None),
                "score": getattr(ctx, "score", 0),
                "match_reason": getattr(ctx, "match_reason", ""),
                "category": getattr(ctx, "category", ""),
                "relations": getattr(ctx, "relations", []),
            }

        memories = [_ctx(m) for m in getattr(result, "memories", [])]
        resources = [_ctx(r) for r in getattr(result, "resources", [])]
        skills = [_ctx(s) for s in getattr(result, "skills", [])]
        return {
            "memories": memories,
            "resources": resources,
            "skills": skills,
            "query_plan": getattr(result, "query_plan", None),
            "total": len(memories) + len(resources) + len(skills),
        }

    def abstract(self, uri):
        return _ov_run(self._client.abstract(uri))

    def overview(self, uri):
        return _ov_run(self._client.overview(uri))

    def read(self, uri):
        return _ov_run(self._client.read(uri))

    def add_resource(self, path, reason="", wait=False):
        return _ov_run(self._client.add_resource(path, reason=reason, wait=wait))

    def ls(self, uri, **kwargs):
        return _ov_run(self._client.ls(uri, **kwargs))

    def grep(self, uri, pattern):
        return _ov_run(self._client.grep(uri, pattern, case_insensitive=True))

    def wait_processed(self, timeout=30):
        return _ov_run(self._client.wait_processed(timeout=timeout))

    def link(self, from_uri, to_uris, reason=""):
        _ov_run(self._client.link(from_uri, to_uris, reason))

    def create_session(self) -> dict:
        return _ov_run(self._client.create_session())

    def session_add_message(self, session_id: str, role: str, text: str):
        log.debug("embedded session_add_message: sid=%s role=%s", session_id, role)

    def session_used(self, session_id: str, uris: list):
        log.debug("embedded session_used: sid=%s uris=%d", session_id, len(uris))

    def session_commit(self, session_id: str) -> dict:
        log.debug("embedded session_commit: sid=%s", session_id)
        return {}


class _OVClient:
    """Private OV dual-mode client used by OpenVikingBackend."""

    def __init__(self, base_url: str | None = None):
        url = base_url or os.environ.get("OV_BASE_URL", "").strip()
        self._impl: _HTTPClient | _EmbeddedClient
        if url:
            self._impl = _HTTPClient(url)
            self._mode = "http"
            log.info("OV HTTP mode: %s", url)
        else:
            self._impl = _EmbeddedClient()
            self._mode = "embedded"

    @property
    def mode(self):
        return self._mode

    def health(self):
        return self._impl.health()

    def find(self, query, limit=10, target_uri=""):
        return self._impl.find(query, limit=limit, target_uri=target_uri)

    def search(self, query, session_id=None, limit=10):
        return self._impl.search(query, session_id=session_id, limit=limit)

    def abstract(self, uri):
        return self._impl.abstract(uri)

    def overview(self, uri):
        return self._impl.overview(uri)

    def read(self, uri):
        return self._impl.read(uri)

    def add_resource(self, path, reason="", wait=False):
        return self._impl.add_resource(path, reason=reason, wait=wait)

    def ls(self, uri, **kwargs):
        return self._impl.ls(uri, **kwargs)

    def grep(self, uri, pattern):
        return self._impl.grep(uri, pattern)

    def wait_processed(self, timeout=30):
        return self._impl.wait_processed(timeout=timeout)

    def link(self, from_uri, to_uris, reason=""):
        return self._impl.link(from_uri, to_uris, reason)

    @property
    def _client(self):
        if self._mode == "embedded":
            return self._impl.raw
        raise AttributeError("HTTP mode does not support direct _client access")


class OpenVikingBackend(KnowledgeBackend):
    """OV backend with auto-selection: embedded (default) or HTTP mode.

    Set OV_BASE_URL env var to use HTTP mode (remote OV serve).
    Otherwise uses AsyncOpenViking embedded mode.
    """

    def __init__(self, base_url: str | None = None):
        self._ov = _OVClient(base_url=base_url)
        # Per-session URI tracking for active_count fix on commit
        self._session_used_uris: dict[str, list] = {}

    @property
    def name(self) -> str:
        return f"OpenViking ({self._ov.mode})"

    @property
    def supports_sessions(self) -> bool:
        return True

    @property
    def supports_llm_search(self) -> bool:
        return True

    @property
    def supports_tiered_loading(self) -> bool:
        return True

    def health(self) -> bool:
        return self._ov.health()

    def find(self, query: str, limit: int = 10) -> SearchResponse:
        raw = self._ov.find(query, limit=limit)
        return self._to_response(raw)

    def search(self, query: str, limit: int = 10, session_id: str | None = None) -> SearchResponse:
        raw = self._ov.search(query, session_id=session_id, limit=limit)
        return self._to_response(raw)

    def abstract(self, uri: str) -> str:
        return self._ov.abstract(uri)

    def overview(self, uri: str) -> str:
        return self._ov.overview(uri)

    def read(self, uri: str) -> str:
        return self._ov.read(uri)

    def ingest(self, content: str, title: str = "", metadata: dict | None = None) -> str:
        """Write content to a temp .md file, then add_resource to OV.

        Note: *metadata* is accepted for interface compatibility but currently
        ignored — OV's ``add_resource`` does not support arbitrary metadata.
        """
        # Replace non-alnum (except -) with _, then collapse runs.
        # OV normalises spaces→_ in URIs; keeping spaces in filenames
        # causes underscore-space mismatches (OV-5).
        safe_title = "".join(c if c.isalnum() or c == "-" else "_" for c in (title or "untitled"))[:60]
        safe_title = re.sub(r"_+", "_", safe_title).strip("_") or "untitled"
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".md",
            prefix=f"{safe_title}_",
            dir=CURATED_DIR,
            delete=False,
        ) as f:
            f.write(content)
            tmp_path = f.name

        uri = ""
        try:
            result = self._ov.add_resource(tmp_path, reason=title or "curator_ingest")
            if isinstance(result, dict) and result.get("root_uri"):
                uri = result["root_uri"]
            self._ov.wait_processed(timeout=30)
        except Exception as e:
            log.warning("ingest failed or timed out: %s", e)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        return uri

    def wait_indexed(self, timeout: int = 30):
        self._ov.wait_processed(timeout=timeout)

    def delete(self, uri: str) -> bool:
        # OV AsyncOpenViking has no delete method; HTTP mode can use fs DELETE
        if self._ov.mode == "http":
            try:
                self._ov._impl._request("DELETE", "/api/v1/fs", params={"uri": uri})  # type: ignore[union-attr]
                return True
            except Exception as e:
                log.debug("failed to delete URI %s via HTTP: %s", uri, e)
                return False
        return False

    def list_resources(self, prefix: str = "") -> list[str]:
        try:
            uri = prefix or "viking://resources"
            items = self._ov.ls(uri)
            return [item.get("uri", item) if isinstance(item, dict) else str(item) for item in items]
        except Exception as e:
            log.debug("failed to list resources with prefix %r: %s", prefix, e)
            return []

    # ── Session tracking ──

    def _session_exists(self, sid: str) -> bool:
        """Check if session exists using OV's official API (v0.2.1+)."""
        try:
            return _ov_run(self._ov._client.session_exists(sid))
        except AttributeError:
            # HTTP mode: _client raises AttributeError; assume session is valid
            log.debug("session_exists not available (HTTP mode?), assuming valid")
            return True
        except Exception as e:
            log.debug("session_exists check failed: %s", e)
            return False

    def load_or_create_session(self, session_file: str = "") -> str:
        """Load existing session from *session_file* or create a new one."""
        if session_file and os.path.exists(session_file):
            try:
                with open(session_file, encoding="utf-8") as f:
                    sid = f.read().strip()
                if sid:
                    if not self._session_exists(sid):
                        log.info(
                            "session %s not found in OV data (data reset?), creating new session",
                            sid,
                        )
                        sid = ""

                    if sid:
                        log.info("复用 session: %s", sid)
                        return sid
            except OSError:
                pass

        sid = self.create_session()

        if session_file and sid:
            try:
                from .file_lock import locked_write

                locked_write(session_file, sid)
            except Exception as e:
                log.debug("failed to persist session ID: %s", e)

        return sid

    def create_session(self) -> str:
        if self._ov.mode == "http":
            result = self._ov._impl.create_session()
        else:
            result = _ov_run(self._ov._client.create_session())
        sid = result.get("session_id", "")
        if sid:
            log.info("新建 session: %s", sid)
        return sid

    def session_add_message(self, session_id: str, role: str, text: str):
        if self._ov.mode == "http":
            self._ov._impl.session_add_message(session_id, role, text)
        else:
            try:
                _ov_run(self._ov._client.session_add_message(session_id, role, text))
            except Exception as e:
                log.debug("failed to add session message (embedded mode) for session %s: %s", session_id, e)

    def session_used(self, session_id: str, uris: list[str]):
        # Track URIs for active_count fix applied during commit
        if session_id not in self._session_used_uris:
            self._session_used_uris[session_id] = []
        self._session_used_uris[session_id].extend(uris)
        if self._ov.mode == "http":
            self._ov._impl.session_used(session_id, list(uris))

    def session_commit(self, session_id: str) -> dict:
        # Peek first; pop only after successful commit so URIs aren't silently
        # discarded when the underlying commit call fails.
        used = list(self._session_used_uris.get(session_id, []))

        result: dict = {}
        commit_ok = False
        if self._ov.mode == "http":
            try:
                result = self._ov._impl.session_commit(session_id)
                commit_ok = True
            except Exception as e:
                log.debug("HTTP session_commit failed: %s", e)
        else:
            # Embedded mode: commit via session object
            try:
                session_obj = _ov_run(self._ov._client.session(session_id))
                if hasattr(session_obj, "load"):
                    session_obj.load()
                raw = _ov_run(session_obj.commit())
                result = raw if isinstance(raw, dict) else {}
                commit_ok = True
            except Exception as e:
                log.debug("embedded session_commit failed: %s", e)

        if commit_ok:
            self._session_used_uris.pop(session_id, None)

        # Workaround: OV's native active_count update is broken in some versions.
        # Only run if commit actually succeeded to avoid incrementing on failed commits.
        fixed = self._fix_active_counts(used) if (used and commit_ok) else 0

        log.info(
            "session commit: memories=%s, active_count=%s (fixed=%d), archived=%s",
            result.get("memories_extracted", 0),
            result.get("active_count_updated", 0),
            fixed,
            result.get("archived"),
        )
        return result

    def _fix_active_counts(self, uris: list) -> int:
        """Workaround for OV upstream active_count bug. Embedded mode only."""
        if self._ov.mode != "embedded" or not uris:
            return 0

        try:
            client = self._ov._client
            inner = getattr(client, "_client", None)
            if inner is None:
                log.warning("_fix_active_counts: OV internal API changed (_client missing), skipping")
                return 0
            service = getattr(inner, "_service", None)
            if service is None:
                log.warning("_fix_active_counts: OV internal API changed (_service missing), skipping")
                return 0
            db = getattr(service, "_vikingdb_manager", None)
            if db is None or not hasattr(db, "_get_collection"):
                log.warning("_fix_active_counts: OV internal API changed (_vikingdb_manager missing), skipping")
                return 0
            coll = db._get_collection("context")
        except Exception as e:
            log.warning("_fix_active_counts: cannot access vectordb: %s", e)
            return 0

        updated = 0
        seen_ids: set = set()
        for uri in uris:
            try:
                result = coll.search_by_random(
                    index_name=db.DEFAULT_INDEX_NAME,
                    limit=10,
                    filters={"op": "must", "field": "uri", "conds": [uri]},
                )
                for item in result.data:
                    rec = dict(item.fields) if item.fields else {}
                    rid = item.id
                    if rid in seen_ids or rec.get("uri") != uri:
                        continue
                    seen_ids.add(rid)
                    old_ac = rec.get("active_count", 0) or 0
                    ok = _ov_run(db.update("context", rid, {"active_count": old_ac + 1}))
                    if ok:
                        updated += 1
            except Exception as e:
                log.debug("_fix_active_counts: URI %s failed: %s", uri, e)

        return updated

    # ── Internal ──

    @staticmethod
    def _to_response(raw: dict) -> SearchResponse:
        results = []
        for bucket in ("resources", "memories", "skills"):
            for item in raw.get(bucket, []):
                results.append(
                    SearchResult(
                        uri=item.get("uri", ""),
                        abstract=item.get("abstract", ""),
                        overview=item.get("overview"),
                        score=item.get("score", 0),
                        context_type=item.get("context_type", bucket.rstrip("s")),
                        match_reason=item.get("match_reason", ""),
                        category=item.get("category", ""),
                        relations=item.get("relations", []),
                        metadata=item.get("metadata", {}),
                    )
                )
        results.sort(key=lambda r: r.score, reverse=True)
        return SearchResponse(
            results=results,
            total=raw.get("total", len(results)),
            query_plan=raw.get("query_plan"),
        )

    @property
    def raw_client(self):
        """Access the underlying private OV client for advanced OV-only usage."""
        return self._ov
