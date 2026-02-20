#!/usr/bin/env python3
"""Freshness scan: 遍历 OV 资源，按时效评分分类报告。

用法:
    python3 scripts/freshness_scan.py                    # 默认报告模式
    python3 scripts/freshness_scan.py --json             # 输出 JSON 报告到 data/freshness_report.json
    python3 scripts/freshness_scan.py --act              # 对 stale 资源触发外搜补充
    python3 scripts/freshness_scan.py --check-urls       # 检查 stale/aging 资源内的 URL 可达性
    python3 scripts/freshness_scan.py --act --check-urls # 全部
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from curator.freshness import uri_freshness_score

# ── Constants ──

OV_BASE = os.environ.get("OV_BASE_URL", "http://127.0.0.1:9100")
FRESH_THRESHOLD = 0.8
AGING_THRESHOLD = 0.4

META_RE = re.compile(r"<!--\s*curator_meta:\s*(.+?)\s*-->")
REVIEW_RE = re.compile(r"<!--\s*review_after:\s*(\d{4}-\d{2}-\d{2})\s*-->")
URL_RE = re.compile(r"https?://[^\s)\]>\"']+")


# ── OV HTTP helpers ──

def _ov_get(path: str, timeout: int = 30):
    """GET request to OV API."""
    url = f"{OV_BASE}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def _ov_post(path: str, data: dict, timeout: int = 30):
    """POST request to OV API."""
    url = f"{OV_BASE}{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


# ── Core logic ──

def list_resources() -> list[dict]:
    """List all OV resources via HTTP API."""
    uri = "viking://resources/"
    encoded = urllib.parse.quote(uri, safe=":/")
    data = _ov_get(f"/api/v1/fs/ls?uri={encoded}&simple=false")
    items = data if isinstance(data, list) else data.get("result", [])
    return items


def read_resource_content(uri: str) -> str:
    """Read resource content. Handles directories by listing children."""
    encoded = urllib.parse.quote(uri, safe=":/")
    try:
        data = _ov_get(f"/api/v1/content/read?uri={encoded}")
        result = data.get("result")
        if result:
            return result
    except Exception:
        pass

    # If it's a directory, try listing and reading first child
    try:
        ls_uri = uri.rstrip("/") + "/"
        encoded_ls = urllib.parse.quote(ls_uri, safe=":/")
        children = _ov_get(f"/api/v1/fs/ls?uri={encoded_ls}&simple=false")
        items = children if isinstance(children, list) else children.get("result", [])
        for child in items:
            if not child.get("isDir") and child.get("uri", "").endswith(".md"):
                child_encoded = urllib.parse.quote(child["uri"], safe=":/")
                child_data = _ov_get(f"/api/v1/content/read?uri={child_encoded}")
                r = child_data.get("result")
                if r:
                    return r
    except Exception:
        pass

    return ""


def parse_curator_meta(content: str) -> dict:
    """Parse curator_meta comment from resource content."""
    meta = {}
    m = META_RE.search(content[:500] if content else "")
    if m:
        raw = m.group(1)
        for pair in raw.split():
            if "=" in pair:
                k, v = pair.split("=", 1)
                meta[k] = v

    m2 = REVIEW_RE.search(content[:500] if content else "")
    if m2:
        meta["review_after"] = m2.group(1)

    return meta


def score_resource(resource: dict, content: str = "") -> dict:
    """Score a single resource for freshness."""
    uri = resource.get("uri", "")
    abstract = resource.get("abstract", "")
    meta = parse_curator_meta(content)

    score = uri_freshness_score(uri, meta)

    # Determine category
    if score >= FRESH_THRESHOLD:
        category = "fresh"
    elif score >= AGING_THRESHOLD:
        category = "aging"
    else:
        category = "stale"

    # Check review_after
    review_after = meta.get("review_after")
    review_expired = False
    if review_after:
        try:
            review_date = datetime.date.fromisoformat(review_after)
            review_expired = review_date <= datetime.date.today()
        except ValueError:
            pass

    return {
        "uri": uri,
        "score": score,
        "category": category,
        "abstract": abstract[:120] if abstract else "",
        "meta": meta,
        "review_after": review_after,
        "review_expired": review_expired,
    }


def scan_all(resources: list[dict] = None) -> list[dict]:
    """Scan all resources and return scored results."""
    if resources is None:
        resources = list_resources()

    results = []
    for res in resources:
        uri = res.get("uri", "")
        # Read content for curator_meta (only first 500 chars needed)
        content = ""
        if res.get("isDir"):
            content = read_resource_content(uri)
        else:
            encoded = urllib.parse.quote(uri, safe=":/")
            try:
                data = _ov_get(f"/api/v1/content/read?uri={encoded}")
                content = data.get("result", "") or ""
            except Exception:
                pass

        scored = score_resource(res, content)
        results.append(scored)

    return results


def categorize(results: list[dict]) -> dict[str, list[dict]]:
    """Split results into fresh/aging/stale categories."""
    cats = {"fresh": [], "aging": [], "stale": []}
    for r in results:
        cats[r["category"]].append(r)
    return cats


# ── URL check ──

def extract_urls_from_content(content: str) -> list[str]:
    """Extract HTTP(S) URLs from content."""
    if not content:
        return []
    urls = URL_RE.findall(content)
    # Clean trailing punctuation
    cleaned = []
    for u in urls:
        u = u.rstrip(".,;:!?)")
        if len(u) > 10:
            cleaned.append(u)
    return list(dict.fromkeys(cleaned))  # dedup, preserve order


def check_url(url: str, timeout: int = 5) -> dict:
    """HEAD-check a URL, return status info."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        req.add_header("User-Agent", "Mozilla/5.0 (Curator-Freshness-Scan)")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"url": url, "ok": True, "status": resp.status}
    except urllib.error.HTTPError as e:
        return {"url": url, "ok": e.code < 400, "status": e.code}
    except Exception as e:
        return {"url": url, "ok": False, "status": 0, "error": str(e)[:120]}


def check_urls_for_resources(scored_results: list[dict], categories: list[str] = None) -> dict[str, list[dict]]:
    """Check URL reachability for resources in given categories.

    Returns: {uri: [url_check_result, ...]}
    """
    if categories is None:
        categories = ["stale", "aging"]

    url_results = {}
    targets = [r for r in scored_results if r["category"] in categories]

    for res in targets:
        uri = res["uri"]
        content = read_resource_content(uri)
        urls = extract_urls_from_content(content)
        if not urls:
            continue

        checks = []
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(check_url, u): u for u in urls[:10]}
            for f in as_completed(futures):
                checks.append(f.result())

        url_results[uri] = checks

    return url_results


# ── Act mode: re-search stale resources ──

def extract_topic(uri: str, abstract: str, content: str = "") -> str:
    """Extract topic/keywords from resource for re-search."""
    # Use abstract if available
    text = abstract or content[:500]
    if not text:
        # Fallback: extract from URI
        name = uri.split("/")[-1]
        name = re.sub(r"^\d+_", "", name)
        return name.replace("_", " ")
    # Take first sentence or first 100 chars as topic
    first_line = text.split("\n")[0].strip("# ").strip()
    return first_line[:100] if first_line else text[:100]


def act_on_stale(stale_results: list[dict]) -> list[dict]:
    """For stale resources, trigger external search and potential re-ingest."""
    # Import pipeline components
    os.environ.setdefault("OPENVIKING_CONFIG_FILE",
                          str(os.path.expanduser("~/.openviking/ov.conf")))

    from curator.pipeline_v2 import run as pipeline_run

    actions = []
    for res in stale_results:
        uri = res["uri"]
        abstract = res.get("abstract", "")
        topic = extract_topic(uri, abstract)

        print(f"  🔍 Re-searching: {topic[:60]}...")
        try:
            result = pipeline_run(topic)
            ingested = result.get("meta", {}).get("ingested", False)
            actions.append({
                "uri": uri,
                "topic": topic,
                "action": "re-searched",
                "ingested": ingested,
                "coverage": result.get("meta", {}).get("coverage", 0),
            })
            if ingested:
                print(f"    ✅ New content ingested for: {topic[:50]}")
            else:
                print(f"    ℹ️  No new content found for: {topic[:50]}")
        except Exception as e:
            actions.append({
                "uri": uri,
                "topic": topic,
                "action": "error",
                "error": str(e)[:200],
            })
            print(f"    ❌ Error: {e}")

    return actions


# ── Report ──

def print_report(categories: dict, url_results: dict = None, actions: list = None):
    """Print human-readable report to stdout."""
    fresh = categories["fresh"]
    aging = categories["aging"]
    stale = categories["stale"]
    total = len(fresh) + len(aging) + len(stale)

    print(f"\n{'='*60}")
    print(f"  📊 Freshness Scan Report — {datetime.date.today().isoformat()}")
    print(f"{'='*60}")
    print(f"  Total resources: {total}")
    print(f"  🟢 Fresh (≥{FRESH_THRESHOLD}):  {len(fresh)}")
    print(f"  🟡 Aging ({AGING_THRESHOLD}-{FRESH_THRESHOLD}): {len(aging)}")
    print(f"  🔴 Stale (<{AGING_THRESHOLD}):  {len(stale)}")

    if stale:
        print(f"\n{'─'*60}")
        print("  🔴 Stale Resources:")
        for r in sorted(stale, key=lambda x: x["score"]):
            expired_tag = " ⚠️EXPIRED" if r.get("review_expired") else ""
            print(f"    score={r['score']:.2f}  {r['uri']}{expired_tag}")
            if r.get("abstract"):
                print(f"      {r['abstract'][:80]}...")

    if aging:
        print(f"\n{'─'*60}")
        print("  🟡 Aging Resources:")
        for r in sorted(aging, key=lambda x: x["score"]):
            expired_tag = " ⚠️EXPIRED" if r.get("review_expired") else ""
            print(f"    score={r['score']:.2f}  {r['uri']}{expired_tag}")

    # Review-expired resources (across all categories)
    expired = [r for cat in categories.values() for r in cat if r.get("review_expired")]
    if expired:
        print(f"\n{'─'*60}")
        print(f"  ⚠️  Review-expired resources ({len(expired)}):")
        for r in expired:
            print(f"    [{r['category']}] {r['uri']}  review_after={r.get('review_after')}")

    if url_results:
        print(f"\n{'─'*60}")
        print("  🔗 URL Check Results:")
        broken_total = 0
        for uri, checks in url_results.items():
            broken = [c for c in checks if not c.get("ok")]
            if broken:
                broken_total += len(broken)
                print(f"    {uri}:")
                for c in broken:
                    status = c.get("status", "?")
                    err = c.get("error", "")
                    print(f"      ❌ [{status}] {c['url'][:80]} {err}")
        if broken_total == 0:
            print("    ✅ All checked URLs are reachable")
        else:
            print(f"    Total broken URLs: {broken_total}")

    if actions:
        print(f"\n{'─'*60}")
        print("  🔄 Re-search Actions:")
        ingested = [a for a in actions if a.get("ingested")]
        errors = [a for a in actions if a.get("action") == "error"]
        print(f"    Total: {len(actions)}, Ingested: {len(ingested)}, Errors: {len(errors)}")
        for a in actions:
            status = "✅" if a.get("ingested") else ("❌" if a.get("action") == "error" else "ℹ️")
            print(f"    {status} {a.get('topic', '')[:60]}")

    print(f"\n{'='*60}\n")


def generate_json_report(
    categories: dict,
    url_results: dict = None,
    actions: list = None,
) -> dict:
    """Generate full JSON report."""
    fresh = categories["fresh"]
    aging = categories["aging"]
    stale = categories["stale"]

    report = {
        "scan_date": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "summary": {
            "total": len(fresh) + len(aging) + len(stale),
            "fresh": len(fresh),
            "aging": len(aging),
            "stale": len(stale),
            "review_expired": sum(
                1 for cat in categories.values() for r in cat if r.get("review_expired")
            ),
        },
        "resources": {
            "fresh": fresh,
            "aging": aging,
            "stale": stale,
        },
    }

    if url_results is not None:
        broken = {}
        for uri, checks in url_results.items():
            bad = [c for c in checks if not c.get("ok")]
            if bad:
                broken[uri] = bad
        report["url_checks"] = {
            "checked_resources": len(url_results),
            "total_urls_checked": sum(len(v) for v in url_results.values()),
            "broken_urls": broken,
        }

    if actions is not None:
        report["actions"] = actions

    return report


# ── Main ──

def main():
    ap = argparse.ArgumentParser(description="OV resource freshness scanner")
    ap.add_argument("--json", action="store_true", help="Output JSON report to data/freshness_report.json")
    ap.add_argument("--act", action="store_true", help="Re-search stale resources")
    ap.add_argument("--check-urls", action="store_true", help="Check URL reachability in stale/aging resources")
    ap.add_argument("--ov-url", default=None, help="OV serve URL (default: $OV_BASE_URL or http://127.0.0.1:9100)")
    args = ap.parse_args()

    if args.ov_url:
        global OV_BASE
        OV_BASE = args.ov_url

    print("🔍 Scanning OV resources for freshness...")
    resources = list_resources()
    print(f"   Found {len(resources)} resources")

    results = scan_all(resources)
    categories = categorize(results)

    # URL check
    url_results = None
    if args.check_urls:
        print("🔗 Checking URLs in stale/aging resources...")
        url_results = check_urls_for_resources(results, ["stale", "aging"])

    # Act mode
    actions = None
    if args.act:
        stale = categories["stale"]
        if stale:
            print(f"🔄 Re-searching {len(stale)} stale resources...")
            actions = act_on_stale(stale)
        else:
            print("✅ No stale resources to re-search")

    # Report
    print_report(categories, url_results, actions)

    # JSON output
    if args.json:
        report = generate_json_report(categories, url_results, actions)
        out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "freshness_report.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"📄 JSON report saved to: {out_path}")


if __name__ == "__main__":
    main()
