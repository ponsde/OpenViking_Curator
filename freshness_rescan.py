#!/usr/bin/env python3
"""
Source-level freshness rescan (v0.5)
- scan curated markdown files for source URLs
- fetch Last-Modified / ETag / status
- save freshness metadata for ranking/inspection
- NEW: scan TTL metadata (curator_meta comments) and flag expired docs
"""

import re
import json
import time
import argparse
import datetime
from pathlib import Path
from urllib.parse import urlparse
import requests

URL_RE = re.compile(r"https?://[^\s)\]>]+")
META_RE = re.compile(r"<!--\s*curator_meta:\s*(.+?)\s*-->")
REVIEW_RE = re.compile(r"<!--\s*review_after:\s*(\d{4}-\d{2}-\d{2})\s*-->")


def extract_urls(text: str):
    return sorted(set(URL_RE.findall(text or "")))


def head_info(url: str, timeout=12):
    try:
        r = requests.head(url, allow_redirects=True, timeout=timeout)
        if r.status_code >= 400:
            # fallback GET for some hosts
            r = requests.get(url, stream=True, allow_redirects=True, timeout=timeout)
        return {
            "ok": r.status_code < 400,
            "status": r.status_code,
            "last_modified": r.headers.get("Last-Modified", ""),
            "etag": r.headers.get("ETag", ""),
            "final_url": r.url,
            "host": urlparse(r.url).netloc,
        }
    except Exception as e:
        return {
            "ok": False,
            "status": 0,
            "last_modified": "",
            "etag": "",
            "final_url": url,
            "host": urlparse(url).netloc,
            "error": str(e)[:160],
        }


def score_freshness(item: dict):
    if not item.get("ok"):
        return 0.2
    # if server exposes freshness metadata, score higher
    if item.get("last_modified") or item.get("etag"):
        return 0.9
    # reachable but no metadata
    return 0.6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--curated-dir", default="cases")
    ap.add_argument("--output", default="output/freshness.json")
    ap.add_argument("--ttl-scan", action="store_true", help="扫描 TTL metadata，报告过期文档")
    args = ap.parse_args()

    cdir = Path(args.curated_dir)
    outp = Path(args.output)
    outp.parent.mkdir(parents=True, exist_ok=True)

    files = list(cdir.glob("*.md")) if cdir.exists() else []

    # TTL 扫描模式
    if args.ttl_scan:
        today = datetime.date.today()
        expired = []
        soon = []  # 7天内过期
        ok = []
        no_meta = []

        for f in files:
            txt = f.read_text(encoding="utf-8", errors="ignore")[:500]
            review_m = REVIEW_RE.search(txt)
            meta_m = META_RE.search(txt)

            if not review_m:
                no_meta.append(str(f))
                continue

            review_date = datetime.date.fromisoformat(review_m.group(1))
            meta_info = meta_m.group(1) if meta_m else ""

            entry = {
                "file": str(f),
                "review_after": review_m.group(1),
                "meta": meta_info,
            }

            if review_date <= today:
                expired.append(entry)
            elif (review_date - today).days <= 7:
                soon.append(entry)
            else:
                ok.append(entry)

        ttl_result = {
            "scan_date": today.isoformat(),
            "total_files": len(files),
            "expired": len(expired),
            "expiring_soon": len(soon),
            "ok": len(ok),
            "no_metadata": len(no_meta),
            "expired_files": expired,
            "expiring_soon_files": soon,
        }
        print(json.dumps(ttl_result, ensure_ascii=False, indent=2))
        return

    # 原有 URL 扫描模式
    source_map = {}

    for f in files:
        txt = f.read_text(encoding="utf-8", errors="ignore")
        urls = extract_urls(txt)
        if not urls:
            continue
        source_map[str(f)] = []
        for u in urls[:20]:
            info = head_info(u)
            info["freshness_score"] = score_freshness(info)
            source_map[str(f)].append(info)
            time.sleep(0.05)

    summary = {
        "scanned_files": len(files),
        "files_with_urls": len(source_map),
        "total_urls": sum(len(v) for v in source_map.values()),
        "ok_urls": sum(1 for v in source_map.values() for x in v if x.get("ok")),
        "updated_at": int(time.time()),
    }

    out = {"summary": summary, "sources": source_map}
    outp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
