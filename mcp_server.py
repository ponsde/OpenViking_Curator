#!/usr/bin/env python3
"""
OpenViking Curator — MCP Server (stdio)

标准 MCP (Model Context Protocol) server，通过 stdio JSON-RPC 通信。
工具:
  - curator_query: 知识查询（含门控、检索、外搜、回答全流程）
  - curator_ingest: 手动入库一条知识
  - curator_status: 查看知识库状态

用法:
  python3 mcp_server.py          # stdio 模式（给 mcporter / Claude Desktop 用）
  mcporter call --stdio "python3 mcp_server.py" curator_query query="MCP 是什么"
"""

import json
import sys
import os
import io
import traceback
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr

# 加载 .env
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

# ── MCP Protocol Constants ──
JSONRPC = "2.0"

SERVER_INFO = {
    "name": "openviking-curator",
    "version": "0.7.0",
}

CAPABILITIES = {
    "tools": {},
}

TOOLS = [
    {
        "name": "curator_query",
        "description": (
            "查询 OpenViking 知识库。自动执行门控判断、本地检索、外部搜索（如需）、"
            "审核入库、冲突检测、生成回答。适用于知识问答、开发经验复用、排查参考等场景。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "用户问题（中/英文均可）",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "curator_ingest",
        "description": "手动向知识库添加一条知识文档（Markdown 格式）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "文档标题",
                },
                "content": {
                    "type": "string",
                    "description": "Markdown 内容",
                },
            },
            "required": ["title", "content"],
        },
    },
    {
        "name": "curator_status",
        "description": "查看知识库资源数量与健康状态。",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


# ── Tool Implementations ──

def _tool_curator_query(args: dict) -> dict:
    query = args.get("query", "").strip()
    if not query:
        return {"error": "query is required"}

    # 门控
    sys.path.insert(0, str(Path(__file__).parent))
    from curator_query import should_route, run_curator

    route, reason = should_route(query)
    if not route:
        return {
            "routed": False,
            "reason": reason,
            "message": "此问题不需要知识库查询，可直接回答。",
        }

    result = run_curator(query)
    # 精简返回，去掉过大的 steps
    if "steps" in result:
        del result["steps"]
    return result



def _tool_curator_ingest(args: dict) -> dict:
    title = args.get("title", "").strip()
    content = args.get("content", "").strip()
    if not title or not content:
        return {"error": "title and content are required"}

    from curator.session_manager import OVClient
    from curator.review import ingest_markdown_v2

    ov = OVClient()
    if not ov.health():
        return {"error": "OV serve not available"}

    try:
        ing = ingest_markdown_v2(ov, title[:60], content)
        return {"success": True, "uri": ing.get("root_uri", "")}
    except Exception as e:
        return {"error": str(e)}


def _tool_curator_status(args: dict) -> dict:
    import json
    import urllib.request

    base = os.environ.get("OV_BASE_URL", "http://127.0.0.1:9100")
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=10) as r:
            health = json.loads(r.read())
        req = urllib.request.Request(
            f"{base}/api/v1/fs/ls?uri=viking://resources/&simple=true",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            ls_result = json.loads(r.read()).get("result", [])
        return {"health": health.get("status", "unknown"), "resource_count": len(ls_result), "base_url": base}
    except Exception as e:
        return {"error": str(e)}


TOOL_DISPATCH = {
    "curator_query": _tool_curator_query,
    "curator_ingest": _tool_curator_ingest,
    "curator_status": _tool_curator_status,
}


# ── JSON-RPC Handler ──

def handle_request(req: dict) -> dict | None:
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params", {})

    # Notifications (no id) — just ack silently
    if req_id is None and method in ("notifications/initialized",):
        return None

    if method == "initialize":
        return {
            "jsonrpc": JSONRPC,
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": SERVER_INFO,
                "capabilities": CAPABILITIES,
            },
        }

    if method == "tools/list":
        return {
            "jsonrpc": JSONRPC,
            "id": req_id,
            "result": {"tools": TOOLS},
        }

    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})
        fn = TOOL_DISPATCH.get(tool_name)
        if not fn:
            return {
                "jsonrpc": JSONRPC,
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
            }

        # 捕获 stdout/stderr，避免 print 干扰 JSON-RPC 通道
        buf_out = io.StringIO()
        buf_err = io.StringIO()
        try:
            with redirect_stdout(buf_out), redirect_stderr(buf_err):
                result = fn(tool_args)
        except Exception as e:
            result = {"error": str(e), "traceback": traceback.format_exc()}

        return {
            "jsonrpc": JSONRPC,
            "id": req_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result, ensure_ascii=False, indent=2),
                    }
                ],
            },
        }

    # Unknown method
    return {
        "jsonrpc": JSONRPC,
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main():
    """stdio JSON-RPC loop"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            resp = {
                "jsonrpc": JSONRPC,
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
            continue

        resp = handle_request(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
