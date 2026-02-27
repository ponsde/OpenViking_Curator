"""OpenViking backend — implements KnowledgeBackend for OV (embedded + HTTP).

This wraps the existing OVClient dual-mode logic. All OV-specific code
(viking:// URIs, AGFS, session objects) lives here, not in the pipeline.
"""

import os
import re
import tempfile

from .backend import KnowledgeBackend, SearchResponse, SearchResult
from .config import CURATED_DIR, log


class OpenVikingBackend(KnowledgeBackend):
    """OV backend with auto-selection: embedded (default) or HTTP mode.

    Set OV_BASE_URL env var to use HTTP mode (remote OV serve).
    Otherwise uses AsyncOpenViking embedded mode.
    """

    def __init__(self, base_url: str | None = None):
        from .session_manager import OVClient

        self._ov = OVClient(base_url=base_url)
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

    def _session_dir_exists(self, sid: str) -> bool:
        """Check if the session directory exists in OV's embedded data store.

        OV's session().load() silently starts a fresh empty session when the
        session_id doesn't exist — there is no validation at the OV API level.
        For embedded mode we can check the filesystem directly to avoid silently
        reusing a stale ID after an OV data reset.

        Uses the same path resolution as session_manager._init_embedded():
            OV_DATA_PATH → CURATOR_DATA_PATH → "./data"
        """
        # Mirror session_manager._DEFAULT_DATA_PATH resolution logic
        data_path = os.environ.get(
            "OV_DATA_PATH",
            os.environ.get("CURATOR_DATA_PATH", "./data"),
        )
        return os.path.isdir(os.path.join(data_path, "viking", "session", sid))

    def load_or_create_session(self, session_file: str = "") -> str:
        """Load existing session from *session_file* or create a new one.

        Persists the session ID to *session_file* so context carries over
        across pipeline runs.

        For embedded mode, validates the session directory still exists in OV
        data before reusing a stored ID — OV data resets would otherwise cause
        the stale ID to be silently accepted as a fresh empty session.
        HTTP mode skips the filesystem check (session lives on OV server).
        """
        if session_file and os.path.exists(session_file):
            try:
                with open(session_file, encoding="utf-8") as f:
                    sid = f.read().strip()
                if sid:
                    # Embedded mode: validate session directory still exists.
                    # OV silently starts a fresh session for unknown IDs, so we
                    # must check the filesystem ourselves.
                    if self._ov.mode != "http" and not self._session_dir_exists(sid):
                        log.info(
                            "session %s not found in OV data (data reset?), creating new session",
                            sid,
                        )
                        sid = ""  # fall through to create_session()

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
        from .session_manager import _ov_run

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
                from .session_manager import _ov_run

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
        from .session_manager import _ov_run

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
        """Workaround for OV upstream active_count bug. Embedded mode only.

        Accesses OV internals via guarded attribute lookups.  If OV upstream
        refactors, this will silently no-op rather than crash.
        """
        from .session_manager import _ov_run

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
        """Access the underlying OVClient (for advanced use cases).

        Returns:
            The wrapped :class:`OVClient` instance.

        Warning:
            Using raw_client bypasses the backend abstraction.
            Only use for OV-specific features not covered by the interface.
        """
        return self._ov
