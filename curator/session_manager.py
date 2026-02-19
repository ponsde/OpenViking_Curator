"""Session manager: OV HTTP API 的 session 生命周期管理。"""

import json
import os
import time
import urllib.request

from .config import log

_DEFAULT_BASE = "http://127.0.0.1:9100"
_COMMIT_MSG_THRESHOLD = 5
_COMMIT_TIME_THRESHOLD = 3600  # 1h


class OVClient:
    """轻量 OV HTTP 客户端，不依赖 openviking 包。"""

    def __init__(self, base_url: str = None):
        self.base = (base_url or os.environ.get("OV_BASE_URL") or _DEFAULT_BASE).rstrip("/")

    def _post(self, path: str, data: dict, timeout: int = 60) -> dict:
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())

    def _get(self, path: str, timeout: int = 30) -> dict:
        with urllib.request.urlopen(f"{self.base}{path}", timeout=timeout) as resp:
            return json.loads(resp.read())

    def health(self) -> bool:
        try:
            r = self._get("/health")
            return r.get("status") == "ok"
        except Exception:
            return False

    # ── 检索 ──

    def find(self, query: str, limit: int = 10, target_uri: str = "") -> dict:
        """纯语义检索，不需要 session。快（~1.5s）。"""
        r = self._post("/api/v1/search/find", {
            "query": query, "limit": limit, "target_uri": target_uri,
        })
        return r.get("result", r)

    def search(self, query: str, session_id: str = None, limit: int = 10) -> dict:
        """VLM 意图分析 + 层次化检索。需要 session_id。慢（~10s）但更准。"""
        payload = {"query": query, "limit": limit}
        if session_id:
            payload["session_id"] = session_id
        r = self._post("/api/v1/search/search", payload)
        return r.get("result", r)

    # ── 内容分层 ──

    def abstract(self, uri: str) -> str:
        """L0 摘要 ~100 token。"""
        r = self._get(f"/api/v1/content/abstract?uri={urllib.request.quote(uri, safe='/:')}")
        return r.get("result", "")

    def overview(self, uri: str) -> str:
        """L1 概览 ~2k token。"""
        r = self._get(f"/api/v1/content/overview?uri={urllib.request.quote(uri, safe='/:')}")
        return r.get("result", "")

    def read(self, uri: str) -> str:
        """L2 全文。"""
        r = self._get(f"/api/v1/content/read?uri={urllib.request.quote(uri, safe='/:')}")
        return r.get("result", "")

    # ── 资源管理 ──

    def add_resource(self, path: str, reason: str = "", wait: bool = False) -> dict:
        r = self._post("/api/v1/resources", {
            "path": path, "reason": reason, "wait": wait,
        })
        return r.get("result", r)

    def grep(self, uri: str, pattern: str) -> dict:
        r = self._post("/api/v1/search/grep", {
            "uri": uri, "pattern": pattern, "case_insensitive": True,
        })
        return r.get("result", r)

    # ── Session ──

    def create_session(self) -> str:
        r = self._post("/api/v1/sessions", {})
        return r["result"]["session_id"]

    def add_message(self, session_id: str, role: str, content: str):
        self._post(f"/api/v1/sessions/{session_id}/messages", {
            "role": role, "content": content,
        })

    def commit(self, session_id: str) -> dict:
        r = self._post(f"/api/v1/sessions/{session_id}/commit", {})
        return r.get("result", r)

    # ── 关系 ──

    def wait_processed(self, timeout: int = 30) -> dict:
        """等待 OV 处理队列完成（入库后索引建立）。"""
        r = self._post("/api/v1/system/wait", {"timeout": timeout}, timeout=timeout + 10)
        return r

    def link(self, from_uri: str, to_uris: list, reason: str = ""):
        self._post("/api/v1/relations/link", {
            "from_uri": from_uri, "to_uris": to_uris, "reason": reason,
        })


class SessionManager:
    """管理 Curator 的持久 session。"""

    def __init__(self, ov: OVClient, session_id_file: str = None):
        self.ov = ov
        self._sid_file = session_id_file or os.path.join(
            os.environ.get("CURATOR_DATA_PATH", "./data"), ".curator_session_id"
        )
        self._sid = self._load_or_create()
        self._msg_count = 0
        self._last_commit = time.time()

    def _load_or_create(self) -> str:
        if os.path.exists(self._sid_file):
            sid = open(self._sid_file).read().strip()
            if sid:
                log.info("复用 session: %s", sid)
                return sid
        sid = self.ov.create_session()
        os.makedirs(os.path.dirname(self._sid_file) or ".", exist_ok=True)
        open(self._sid_file, "w").write(sid)
        log.info("新建 session: %s", sid)
        return sid

    @property
    def session_id(self) -> str:
        return self._sid

    def add_user_query(self, query: str):
        """记录用户提问到 session。"""
        try:
            self.ov.add_message(self._sid, "user", query)
            self._msg_count += 1
        except Exception as e:
            log.debug("add user msg 失败: %s", e)

    def add_assistant_response(self, answer: str, used_uris: list = None):
        """记录回答到 session。"""
        try:
            # 截断太长的回答
            content = answer[:800]
            if used_uris:
                content += "\n\n引用: " + ", ".join(used_uris[:5])
            self.ov.add_message(self._sid, "assistant", content)
            self._msg_count += 1
        except Exception as e:
            log.debug("add assistant msg 失败: %s", e)

    def search(self, query: str, limit: int = 10) -> dict:
        """通过当前 session 做 VLM 智能检索。"""
        try:
            return self.ov.search(query, session_id=self._sid, limit=limit)
        except Exception as e:
            log.warning("session search 失败，降级 find: %s", e)
            return self.ov.find(query, limit=limit)

    def maybe_commit(self):
        """达到条件时 commit（提取记忆）。"""
        elapsed = time.time() - self._last_commit
        if self._msg_count >= _COMMIT_MSG_THRESHOLD or elapsed >= _COMMIT_TIME_THRESHOLD:
            try:
                result = self.ov.commit(self._sid)
                log.info("session commit: extracted=%s, archived=%s",
                         result.get("memories_extracted", 0), result.get("archived"))
                self._msg_count = 0
                self._last_commit = time.time()
            except Exception as e:
                log.debug("commit 失败: %s", e)
