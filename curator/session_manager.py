"""Session manager: AsyncOpenViking 嵌入模式。

替代之前的 HTTP 模式，使用 OV 原生 API，支持：
- search(session_id) — VLM 意图分析 + 三路检索
- session.used() — 记录使用的 URI，commit 时更新 active_count
- session.commit() — 压缩对话 + 提取长期记忆
"""

import asyncio
import os
import threading
import time

from .config import log

_DEFAULT_DATA_PATH = os.environ.get(
    "OV_DATA_PATH",
    os.path.expanduser("~/OpenViking_test/data"),
)

# ── 独立 event loop，避免和 OV 内部的 run_async background loop 冲突 ──

_ov_loop: asyncio.AbstractEventLoop | None = None
_ov_thread: threading.Thread | None = None
_ov_lock = threading.Lock()


def _get_ov_loop() -> asyncio.AbstractEventLoop:
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
    """在独立 loop 上执行 OV async 调用。不会和 OV 内部 run_async 死锁。"""
    loop = _get_ov_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=120)


# ── 单例 AsyncOpenViking 客户端 ──

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


class OVClient:
    """OV 客户端 — 嵌入模式，接口兼容原 HTTP 版本。"""

    def __init__(self, base_url: str = None):
        # base_url 参数保留兼容性但不再使用
        self._client = _get_async_client()

    def health(self) -> bool:
        try:
            return self._client.is_healthy()
        except Exception:
            return False

    # ── 检索 ──

    def find(self, query: str, limit: int = 10, target_uri: str = "") -> dict:
        """纯语义检索，不需要 session。快（~1s）。"""
        result = _ov_run(self._client.find(query, target_uri=target_uri, limit=limit))
        return self._find_result_to_dict(result)

    def search(self, query: str, session_id: str = None, limit: int = 10) -> dict:
        """VLM 意图分析 + 三路检索。可带 session 做上下文感知。"""
        result = _ov_run(self._client.search(
            query, session_id=session_id, limit=limit,
        ))
        return self._find_result_to_dict(result)

    @staticmethod
    def _find_result_to_dict(result) -> dict:
        """把 FindResult 对象转为 dict（兼容原 HTTP 返回格式）。"""
        def _ctx_to_dict(ctx):
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
        memories = [_ctx_to_dict(m) for m in getattr(result, "memories", [])]
        resources = [_ctx_to_dict(r) for r in getattr(result, "resources", [])]
        skills = [_ctx_to_dict(s) for s in getattr(result, "skills", [])]
        return {
            "memories": memories,
            "resources": resources,
            "skills": skills,
            "query_plan": getattr(result, "query_plan", None),
            "total": len(memories) + len(resources) + len(skills),
        }

    # ── 内容分层 ──

    def abstract(self, uri: str) -> str:
        return _ov_run(self._client.abstract(uri))

    def overview(self, uri: str) -> str:
        return _ov_run(self._client.overview(uri))

    def read(self, uri: str) -> str:
        return _ov_run(self._client.read(uri))

    # ── 资源管理 ──

    def add_resource(self, path: str, reason: str = "", wait: bool = False) -> dict:
        return _ov_run(self._client.add_resource(path, reason=reason, wait=wait))

    def ls(self, uri: str, **kwargs) -> list:
        return _ov_run(self._client.ls(uri, **kwargs))

    def grep(self, uri: str, pattern: str) -> dict:
        return _ov_run(self._client.grep(uri, pattern, case_insensitive=True))

    def wait_processed(self, timeout: int = 30) -> dict:
        return _ov_run(self._client.wait_processed(timeout=timeout))

    def link(self, from_uri: str, to_uris: list, reason: str = ""):
        _ov_run(self._client.link(from_uri, to_uris, reason))


# ── Session 管理（使用 OV 原生 Session） ──

_COMMIT_MSG_THRESHOLD = 2
_COMMIT_TIME_THRESHOLD = 1800  # 30min


class SessionManager:
    """管理 Curator 的持久 session — 使用 OV 原生 Session 对象。"""

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
            sid = open(self._sid_file).read().strip()
            if sid:
                log.info("复用 session: %s", sid)
                return sid
        # 用 OV 原生创建
        result = _ov_run(self.ov._client.create_session())
        sid = result.get("session_id", "")
        os.makedirs(os.path.dirname(self._sid_file) or ".", exist_ok=True)
        open(self._sid_file, "w").write(sid)
        log.info("新建 session: %s", sid)
        return sid

    def _get_session(self):
        """获取 OV 原生 Session 对象。"""
        if self._session is None:
            self._session = self.ov._client.session(self._sid)
            self._session.load()
        return self._session

    @property
    def session_id(self) -> str:
        return self._sid

    def add_user_query(self, query: str):
        try:
            from openviking.message import TextPart
            s = self._get_session()
            s.add_message("user", [TextPart(text=query)])
            self._msg_count += 1
        except Exception as e:
            log.debug("add user msg 失败: %s", e)

    def add_assistant_response(self, answer: str, used_uris: list = None):
        try:
            from openviking.message import TextPart
            s = self._get_session()
            content = answer[:800]
            if used_uris:
                content += "\n\n引用: " + ", ".join(used_uris[:5])
            s.add_message("assistant", [TextPart(text=content)])
            self._msg_count += 1
            # 记录 used URIs，commit 时传给 OV
            if used_uris:
                self._used_uris.extend(used_uris)
        except Exception as e:
            log.debug("add assistant msg 失败: %s", e)

    def record_used(self, uris: list):
        """显式记录使用了哪些 URI（调 OV session.used）。"""
        if not uris:
            return
        try:
            s = self._get_session()
            s.used(contexts=list(uris))
            log.info("session.used: %d URIs", len(uris))
        except Exception as e:
            log.debug("session.used 失败: %s", e)

    def search(self, query: str, limit: int = 10) -> dict:
        """通过 session 做 VLM 智能检索。"""
        return self.ov.search(query, session_id=self._sid, limit=limit)

    def maybe_commit(self):
        """达到条件时 commit（提取记忆 + 更新 active_count）。"""
        elapsed = time.time() - self._last_commit
        if self._msg_count >= _COMMIT_MSG_THRESHOLD or elapsed >= _COMMIT_TIME_THRESHOLD:
            # 先调 used() 记录本轮使用的 URI
            if self._used_uris:
                self.record_used(self._used_uris)
                self._used_uris = []
            try:
                s = self._get_session()
                result = s.commit()
                log.info("session commit: memories=%s, active_count=%s, archived=%s",
                         result.get("memories_extracted", 0),
                         result.get("active_count_updated", 0),
                         result.get("archived"))
                self._msg_count = 0
                self._last_commit = time.time()
                self._session = None  # commit 后重新 load
            except Exception as e:
                log.debug("commit 失败: %s", e)
