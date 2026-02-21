"""Session manager: 双模式 OV 客户端。

自动选择模式：
- 设置 OV_BASE_URL → HTTP 模式（Docker / 远程 OV）
- 未设置 → 嵌入模式（本地，需要 openviking 包）

接口完全一致，调用方无需关心底层。
"""

import asyncio
import json
import os
import threading
import time
import urllib.request
import urllib.parse

from .config import log

_DEFAULT_DATA_PATH = os.environ.get(
    "OV_DATA_PATH",
    os.environ.get("CURATOR_DATA_PATH", "./data"),
)


# ══════════════════════════════════════════════════
#  HTTP 模式（Docker / 远程）
# ══════════════════════════════════════════════════

class _HTTPClient:
    """通过 OV HTTP serve 的 REST API 交互。"""

    def __init__(self, base_url: str):
        self._base = base_url.rstrip("/")

    def _request(self, method: str, path: str, data: dict = None, params: dict = None, timeout: int = 60):
        """统一请求，自动解包 {"status":"ok","result":...} 格式。"""
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
        # OV serve 统一格式: {"status":"ok","result":..., "error":null}
        if isinstance(resp, dict) and "result" in resp:
            if resp.get("error"):
                raise RuntimeError(f"OV API error: {resp['error']}")
            return resp["result"]
        return resp

    def health(self) -> bool:
        try:
            r = self._request("GET", "/health")
            return r.get("status") == "ok" if isinstance(r, dict) else True
        except Exception:
            return False

    def find(self, query: str, limit: int = 10, target_uri: str = "") -> dict:
        data = {"query": query, "limit": limit}
        if target_uri:
            data["target_uri"] = target_uri
        result = self._request("POST", "/api/v1/search/find", data)
        return self._normalize_find(result)

    def search(self, query: str, session_id: str = None, limit: int = 10) -> dict:
        data = {"query": query, "limit": limit}
        if session_id:
            data["session_id"] = session_id
        result = self._request("POST", "/api/v1/search/search", data, timeout=120)
        return self._normalize_find(result)

    @staticmethod
    def _normalize_find(result: dict) -> dict:
        """确保 find/search 返回格式统一。"""
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
        return self._request("POST", "/api/v1/resources", {
            "path": path, "reason": reason, "wait": wait
        }, timeout=120)

    def ls(self, uri: str, **kwargs) -> list:
        r = self._request("GET", "/api/v1/fs/ls", params={"uri": uri, **kwargs})
        return r if isinstance(r, list) else r.get("items", [])

    def grep(self, uri: str, pattern: str) -> dict:
        return self._request("POST", "/api/v1/search/grep", {"uri": uri, "pattern": pattern})

    def wait_processed(self, timeout: int = 30) -> dict:
        return self._request("POST", "/api/v1/system/wait", {"timeout": timeout}, timeout=timeout + 10)

    def link(self, from_uri: str, to_uris: list, reason: str = ""):
        self._request("POST", "/api/v1/relations/link", {
            "from_uri": from_uri, "to_uris": to_uris, "reason": reason
        })

    def create_session(self) -> dict:
        return self._request("POST", "/api/v1/sessions", {})

    def session_add_message(self, session_id: str, role: str, text: str):
        self._request("POST", f"/api/v1/sessions/{session_id}/messages", {
            "role": role, "content": text
        })

    def session_used(self, session_id: str, uris: list):
        # OV HTTP serve 没有 session.used() 端点
        log.debug("HTTP 模式不支持 session.used()，跳过")

    def session_commit(self, session_id: str) -> dict:
        return self._request("POST", f"/api/v1/sessions/{session_id}/commit", {}, timeout=120)


# ══════════════════════════════════════════════════
#  嵌入模式（本地）
# ══════════════════════════════════════════════════

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
        log.info("OV 嵌入模式初始化完成: %s", data_path)
    return _async_client


class _EmbeddedClient:
    """通过 AsyncOpenViking 嵌入模式直接调用。"""

    def __init__(self):
        self._client = _get_async_client()

    @property
    def raw(self):
        return self._client

    def health(self) -> bool:
        try:
            return self._client.is_healthy()
        except Exception:
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
        # Embedded mode: the AsyncOpenViking session API uses session objects,
        # not raw session_add_message.  This is provided for parity with _HTTPClient.
        # Callers using SessionManager._get_session() handle this differently.
        log.debug("embedded session_add_message: sid=%s role=%s (delegated to session object)", session_id, role)

    def session_used(self, session_id: str, uris: list):
        log.debug("embedded session_used: sid=%s uris=%d (delegated to session object)", session_id, len(uris))

    def session_commit(self, session_id: str) -> dict:
        # In embedded mode, commit is done via session object, not raw API
        log.debug("embedded session_commit: sid=%s (delegated to session object)", session_id)
        return {}


# ══════════════════════════════════════════════════
#  统一入口
# ══════════════════════════════════════════════════

class OVClient:
    """OV 统一客户端 — 自动选择 HTTP 或嵌入模式。"""

    def __init__(self, base_url: str = None):
        url = base_url or os.environ.get("OV_BASE_URL", "").strip()
        if url:
            self._impl = _HTTPClient(url)
            self._mode = "http"
            log.info("OV HTTP 模式: %s", url)
        else:
            self._impl = _EmbeddedClient()
            self._mode = "embedded"

    @property
    def mode(self):
        return self._mode

    def health(self): return self._impl.health()
    def find(self, query, limit=10, target_uri=""): return self._impl.find(query, limit=limit, target_uri=target_uri)
    def search(self, query, session_id=None, limit=10): return self._impl.search(query, session_id=session_id, limit=limit)
    def abstract(self, uri): return self._impl.abstract(uri)
    def overview(self, uri): return self._impl.overview(uri)
    def read(self, uri): return self._impl.read(uri)
    def add_resource(self, path, reason="", wait=False): return self._impl.add_resource(path, reason=reason, wait=wait)
    def ls(self, uri, **kwargs): return self._impl.ls(uri, **kwargs)
    def grep(self, uri, pattern): return self._impl.grep(uri, pattern)
    def wait_processed(self, timeout=30): return self._impl.wait_processed(timeout=timeout)
    def link(self, from_uri, to_uris, reason=""): return self._impl.link(from_uri, to_uris, reason)
    def create_session(self): return self._impl.create_session()
    def session_add_message(self, session_id, role, text): return self._impl.session_add_message(session_id, role, text)
    def session_used(self, session_id, uris): return self._impl.session_used(session_id, uris)
    def session_commit(self, session_id): return self._impl.session_commit(session_id)

    @property
    def _client(self):
        if self._mode == "embedded":
            return self._impl.raw
        raise AttributeError("HTTP 模式不支持直接访问 _client")


# ── Session 管理 ──

_COMMIT_MSG_THRESHOLD = 2
_COMMIT_TIME_THRESHOLD = 1800


class SessionManager:
    """管理 Curator 的持久 session。支持 HTTP 和嵌入两种模式。"""

    def __init__(self, ov: OVClient, session_id_file: str = None):
        self.ov = ov
        self._sid_file = session_id_file or os.path.join(
            os.environ.get("CURATOR_DATA_PATH", "./data"), ".curator_session_id"
        )
        self._sid = self._load_or_create()
        self._session = None
        self._msg_count = 0
        self._last_commit = time.time()
        self._used_uris = []

    def _load_or_create(self) -> str:
        if os.path.exists(self._sid_file):
            with open(self._sid_file, encoding="utf-8") as f:
                sid = f.read().strip()
            if sid:
                log.info("复用 session: %s", sid)
                return sid

        if self.ov.mode == "http":
            result = self.ov._impl.create_session()
        else:
            result = _ov_run(self.ov._client.create_session())

        sid = result.get("session_id", "")
        if not sid:
            raise RuntimeError("OV create_session 返回空 session_id")
        os.makedirs(os.path.dirname(self._sid_file) or ".", exist_ok=True)
        with open(self._sid_file, "w", encoding="utf-8") as f:
            f.write(sid)
        log.info("新建 session: %s", sid)
        return sid

    def _get_session(self):
        if self.ov.mode != "embedded":
            return None
        if self._session is None:
            self._session = self.ov._client.session(self._sid)
            self._session.load()
        return self._session

    @property
    def session_id(self) -> str:
        return self._sid

    def add_user_query(self, query: str):
        try:
            if self.ov.mode == "http":
                self.ov._impl.session_add_message(self._sid, "user", query)
            else:
                from openviking.message import TextPart
                s = self._get_session()
                s.add_message("user", [TextPart(text=query)])
            self._msg_count += 1
        except Exception as e:
            log.debug("add user msg 失败: %s", e)

    def add_assistant_response(self, answer: str, used_uris: list = None):
        try:
            content = answer[:800]
            if used_uris:
                content += "\n\n引用: " + ", ".join(used_uris[:5])

            if self.ov.mode == "http":
                self.ov._impl.session_add_message(self._sid, "assistant", content)
            else:
                from openviking.message import TextPart
                s = self._get_session()
                s.add_message("assistant", [TextPart(text=content)])

            self._msg_count += 1
            if used_uris:
                self._used_uris.extend(used_uris)
        except Exception as e:
            log.debug("add assistant msg 失败: %s", e)

    def record_used(self, uris: list):
        if not uris:
            return
        try:
            if self.ov.mode == "http":
                self.ov._impl.session_used(self._sid, list(uris))
            else:
                s = self._get_session()
                s.used(contexts=list(uris))
            log.info("session.used: %d URIs", len(uris))
        except Exception as e:
            log.debug("session.used 失败: %s", e)

    def _fix_active_counts(self, uris: list) -> int:
        """Workaround for OV upstream bug: session._update_active_counts() passes
        MongoDB-style filter/update kwargs but the backend expects (collection, id, data).

        We query the vectordb directly with search_by_random to find record IDs,
        then call update(collection, id, data) correctly.

        Works in embedded mode only; HTTP mode relies on OV serve (which has the
        same bug but we can't access the vectordb directly).
        """
        if self.ov.mode != "embedded" or not uris:
            return 0

        try:
            # NOTE: 穿透三层私有 API（AsyncOpenViking._client._service._vikingdb_manager）
            # 这是 OV upstream active_count bug 的临时 workaround。
            # OV 上游重构后可能 break，所以用 hasattr 守卫每一层。
            client = self.ov._client
            inner = getattr(client, '_client', None)
            if inner is None:
                log.warning("_fix_active_counts: OV 内部结构变更，_client 不存在，跳过 active_count 修正")
                return 0
            service = getattr(inner, '_service', None)
            if service is None:
                log.warning("_fix_active_counts: OV 内部结构变更，_service 不存在，跳过 active_count 修正")
                return 0
            db = getattr(service, '_vikingdb_manager', None)
            if db is None:
                log.warning("_fix_active_counts: OV 内部结构变更，_vikingdb_manager 不存在，跳过 active_count 修正")
                return 0
            if not hasattr(db, '_get_collection'):
                log.warning("_fix_active_counts: OV 内部结构变更，_get_collection 不存在，跳过 active_count 修正")
                return 0
            coll = db._get_collection("context")
        except AttributeError as e:
            log.warning("_fix_active_counts: OV 内部 API 穿透失败（上游可能已重构）: %s", e)
            return 0
        except Exception as e:
            log.debug("_fix_active_counts: 无法访问 vectordb: %s", e)
            return 0

        updated = 0
        seen_ids = set()
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
                    if rid in seen_ids:
                        continue
                    seen_ids.add(rid)
                    if rec.get("uri") != uri:
                        continue
                    old_ac = rec.get("active_count", 0) or 0
                    ok = _ov_run(db.update("context", rid, {"active_count": old_ac + 1}))
                    if ok:
                        updated += 1
            except Exception as e:
                log.debug("_fix_active_counts: URI %s 失败: %s", uri, e)

        if updated:
            log.info("active_count 修正: %d 条记录已更新", updated)
        return updated

    def search(self, query: str, limit: int = 10) -> dict:
        return self.ov.search(query, session_id=self._sid, limit=limit)

    def maybe_commit(self):
        elapsed = time.time() - self._last_commit
        if self._msg_count >= _COMMIT_MSG_THRESHOLD or elapsed >= _COMMIT_TIME_THRESHOLD:
            committed_uris = list(self._used_uris)
            if self._used_uris:
                self.record_used(self._used_uris)
                self._used_uris = []
            try:
                if self.ov.mode == "http":
                    result = self.ov._impl.session_commit(self._sid)
                else:
                    s = self._get_session()
                    result = s.commit()

                # Workaround: OV's native active_count update is broken,
                # so we fix it ourselves after commit.
                fixed = self._fix_active_counts(committed_uris)

                log.info("session commit: memories=%s, active_count=%s (fixed=%d), archived=%s",
                         result.get("memories_extracted", 0),
                         result.get("active_count_updated", 0),
                         fixed,
                         result.get("archived"))
                self._msg_count = 0
                self._last_commit = time.time()
                self._session = None
            except Exception as e:
                log.debug("commit 失败: %s", e)
