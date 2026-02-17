#!/usr/bin/env python3
import os
import json
import time
from pathlib import Path

import openviking as ov

OPENVIKING_CONFIG_FILE = os.getenv("OPENVIKING_CONFIG_FILE", "/home/ponsde/OpenViking_test/ov.conf")
DATA_PATH = os.getenv("CURATOR_DATA_PATH", "/home/ponsde/OpenViking_test/data")
MEM_COLLECTION = os.getenv("CURATOR_LONG_MEMORY_COLLECTION", "long_memory")


def _client():
    os.environ["OPENVIKING_CONFIG_FILE"] = OPENVIKING_CONFIG_FILE
    c = ov.SyncOpenViking(path=DATA_PATH)
    c.initialize()
    return c


def remember(text: str, tags=None, source: str = "chat"):
    tags = tags or []
    payload = {
        "ts": int(time.time()),
        "source": source,
        "tags": tags,
        "memory": text.strip(),
    }
    md = (
        f"# Memory\n\n"
        f"- ts: {payload['ts']}\n"
        f"- source: {payload['source']}\n"
        f"- tags: {', '.join(tags) if tags else ''}\n\n"
        f"## content\n\n{payload['memory']}\n"
    )

    mem_dir = Path(os.getenv("CURATOR_LONG_MEMORY_DIR", "/home/ponsde/OpenViking_test/long_memory"))
    mem_dir.mkdir(parents=True, exist_ok=True)
    fn = mem_dir / f"{payload['ts']}_memory.md"
    fn.write_text(md, encoding="utf-8")

    c = _client()
    try:
        out = c.add_resource(path=str(fn))
        return {"ok": True, "root_uri": out.get("root_uri", ""), "file": str(fn), "payload": payload}
    finally:
        try:
            c.close()
        except Exception:
            pass


def recall(query: str, limit: int = 8):
    c = _client()
    try:
        res = c.search(query)
        rows = (res or {}).get("results", []) if isinstance(res, dict) else []
        blocks = []
        uris = []
        for r in rows[:limit]:
            u = r.get("uri", "")
            if not u:
                continue
            uris.append(u)
            try:
                txt = str(c.read(u))[:900]
            except Exception:
                txt = ""
            blocks.append({"uri": u, "snippet": txt})
        return {"ok": True, "query": query, "hits": blocks, "uris": uris}
    finally:
        try:
            c.close()
        except Exception:
            pass


def main():
    import argparse

    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("remember")
    p1.add_argument("text")
    p1.add_argument("--tags", default="")
    p1.add_argument("--source", default="chat")

    p2 = sub.add_parser("recall")
    p2.add_argument("query")
    p2.add_argument("--limit", type=int, default=8)

    args = ap.parse_args()
    if args.cmd == "remember":
        tags = [x.strip() for x in args.tags.split(",") if x.strip()]
        print(json.dumps(remember(args.text, tags=tags, source=args.source), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(recall(args.query, limit=args.limit), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
