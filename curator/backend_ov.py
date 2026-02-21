"""OpenViking backend — implements KnowledgeBackend for OV (embedded + HTTP).

This wraps the existing OVClient dual-mode logic. All OV-specific code
(viking:// URIs, AGFS, session objects) lives here, not in the pipeline.
"""

import os
import tempfile
import time

from .backend import KnowledgeBackend, SearchResult, SearchResponse
from .config import log


class OpenVikingBackend(KnowledgeBackend):
    """OV backend with auto-selection: embedded (default) or HTTP mode.

    Set OV_BASE_URL env var to use HTTP mode (remote OV serve).
    Otherwise uses AsyncOpenViking embedded mode.
    """

    def __init__(self, base_url: str = None):
        from .session_manager import OVClient
        self._ov = OVClient(base_url=base_url)

    @property
    def name(self) -> str:
        return f"OpenViking ({self._ov.mode})"

    @property
    def supports_sessions(self) -> bool:
        return True

    @property
    def supports_llm_search(self) -> bool:
        return True

    def health(self) -> bool:
        return self._ov.health()

    def find(self, query: str, limit: int = 10) -> SearchResponse:
        raw = self._ov.find(query, limit=limit)
        return self._to_response(raw)

    def search(self, query: str, limit: int = 10, session_id: str = None) -> SearchResponse:
        raw = self._ov.search(query, session_id=session_id, limit=limit)
        return self._to_response(raw)

    def abstract(self, uri: str) -> str:
        return self._ov.abstract(uri)

    def overview(self, uri: str) -> str:
        return self._ov.overview(uri)

    def read(self, uri: str) -> str:
        return self._ov.read(uri)

    def ingest(self, content: str, title: str = "", metadata: dict = None) -> str:
        """Write content to a temp .md file, then add_resource to OV."""
        safe_title = "".join(c if c.isalnum() or c in "-_ " else "_" for c in (title or "untitled"))[:60]
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", prefix=f"{safe_title}_",
            dir=os.environ.get("CURATOR_CURATED_DIR", "."),
            delete=False,
        ) as f:
            f.write(content)
            tmp_path = f.name

        try:
            self._ov.add_resource(tmp_path, reason=title or "curator_ingest")
            self._ov.wait_processed(timeout=30)
        except Exception as e:
            log.warning("ingest 等索引超时（内容已存入）: %s", e)
        finally:
            # L1: OV add_resource 已拷贝内容，删除临时文件
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        # 注意: 临时文件已在 finally 中删除，返回路径仅供日志/记录用
        return tmp_path

    def wait_indexed(self, timeout: int = 30):
        self._ov.wait_processed(timeout=timeout)

    def delete(self, uri: str) -> bool:
        # OV AsyncOpenViking has no delete method; HTTP mode can use fs DELETE
        if self._ov.mode == "http":
            try:
                self._ov._impl._request("DELETE", "/api/v1/fs", params={"uri": uri})
                return True
            except Exception:
                return False
        return False

    def list_resources(self, prefix: str = "") -> list[str]:
        try:
            uri = prefix or "viking://resources"
            items = self._ov.ls(uri)
            return [item.get("uri", item) if isinstance(item, dict) else str(item) for item in items]
        except Exception:
            return []

    # ── Session tracking ──

    def create_session(self) -> str:
        from .session_manager import _ov_run
        if self._ov.mode == "http":
            result = self._ov._impl.create_session()
        else:
            result = _ov_run(self._ov._client.create_session())
        return result.get("session_id", "")

    def session_add_message(self, session_id: str, role: str, text: str):
        if self._ov.mode == "http":
            self._ov._impl.session_add_message(session_id, role, text)
        else:
            try:
                from .session_manager import _ov_run
                _ov_run(self._ov._client.session_add_message(session_id, role, text))
            except Exception:
                pass  # Embedded mode: caller manages session objects directly

    def session_used(self, session_id: str, uris: list[str]):
        if self._ov.mode == "http":
            self._ov._impl.session_used(session_id, uris)

    def session_commit(self, session_id: str) -> dict:
        if self._ov.mode == "http":
            return self._ov._impl.session_commit(session_id)
        return {}

    # ── Internal ──

    @staticmethod
    def _to_response(raw: dict) -> SearchResponse:
        results = []
        for bucket in ("resources", "memories", "skills"):
            for item in raw.get(bucket, []):
                results.append(SearchResult(
                    uri=item.get("uri", ""),
                    abstract=item.get("abstract", ""),
                    overview=item.get("overview"),
                    score=item.get("score", 0),
                    context_type=item.get("context_type", bucket.rstrip("s")),
                    match_reason=item.get("match_reason", ""),
                    category=item.get("category", ""),
                    relations=item.get("relations", []),
                ))
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
