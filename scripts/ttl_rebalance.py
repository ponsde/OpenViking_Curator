#!/usr/bin/env python3
"""TTL Rebalance: 扫描已入库文档，根据 adopt 信号重新评估 TTL 分档。

扫描 curated 目录中的本地备份文件，交叉比对 feedback_store 中的 adopt 信号，
报告哪些文档的 usage_tier 已经变化（新热门、新冷门），以及建议的 TTL 调整。

用法:
    python scripts/ttl_rebalance.py                    # 默认报告
    python scripts/ttl_rebalance.py --top 20           # 显示前 20 条
    python scripts/ttl_rebalance.py --json             # 输出 JSON 报告
    python scripts/ttl_rebalance.py --tier cold        # 只看 cold tier
"""

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from curator.env_loader import load_env
from scripts.common import META_RE, default_curated_dir, default_data_dir

# ── Constants ──

DEFAULT_CURATED_DIR = default_curated_dir()
DEFAULT_DATA_DIR = default_data_dir()


def _parse_meta(content: str) -> dict:
    """Extract curator_meta fields from file header."""
    m = META_RE.search(content[:500])
    if not m:
        return {}
    meta = {}
    for pair in m.group(1).split():
        if "=" in pair:
            k, v = pair.split("=", 1)
            meta[k] = v
    return meta


def scan(curated_dir: str, filter_tier: str = None) -> list[dict]:
    """Scan curated backups and compute rebalance suggestions.

    Returns list of dicts with current/suggested tier and TTL for each file.
    """
    from curator.feedback_store import load as load_feedback
    from curator.usage_ttl import adjust_ttl, usage_tier

    feedback = load_feedback()
    curated = Path(curated_dir)

    if not curated.exists():
        print(f"  curated 目录不存在: {curated}", file=sys.stderr)
        return []

    results = []
    for fp in sorted(curated.glob("*.md")):
        content = fp.read_text(encoding="utf-8", errors="replace")
        meta = _parse_meta(content)

        if not meta:
            continue

        freshness = meta.get("freshness", "unknown")
        current_ttl = int(meta.get("ttl_days", "0"))
        current_tier = meta.get("usage_tier", "warm")
        ingested = meta.get("ingested", "")

        # Find adopt signal — use all URIs in feedback that match this file
        # Since local backups don't store the OV URI, we check the title
        # extracted from filename and feedback keys.
        # Best-effort: look for URIs in feedback store that have signals.
        # For a more precise match, use the pipeline with uri_hints.
        title_slug = fp.stem.split("_", 1)[-1] if "_" in fp.stem else fp.stem

        # Check all feedback URIs for a match (prefix match on title)
        best_adopt = 0
        matched_uri = ""
        for uri, signals in feedback.items():
            adopt = signals.get("adopt", 0)
            if adopt > best_adopt:
                # Simple heuristic: URI contains a slug similar to title
                uri_lower = uri.lower()
                slug_lower = title_slug.lower().replace("_", " ")
                if any(w in uri_lower for w in slug_lower.split()[:3] if len(w) > 2):
                    best_adopt = adopt
                    matched_uri = uri

        # If no title match, just use the highest adopt across all feedback
        # This is imprecise but useful for a maintenance overview
        if not matched_uri and feedback:
            # Fall back: global max adopt for reporting purposes
            pass

        new_tier = usage_tier(best_adopt)

        # Recompute TTL from freshness base
        ttl_map = {"current": 180, "recent": 90, "unknown": 60, "outdated": 0}
        base_ttl = ttl_map.get(freshness, 60)
        new_ttl = adjust_ttl(base_ttl, new_tier)

        # Age in days
        age_days = 0
        if ingested:
            try:
                d = datetime.date.fromisoformat(ingested)
                age_days = (datetime.date.today() - d).days
            except ValueError:
                pass

        changed = (new_tier != current_tier) or (new_ttl != current_ttl)

        entry = {
            "file": fp.name,
            "freshness": freshness,
            "ingested": ingested,
            "age_days": age_days,
            "current_tier": current_tier,
            "current_ttl": current_ttl,
            "adopt_count": best_adopt,
            "suggested_tier": new_tier,
            "suggested_ttl": new_ttl,
            "delta_days": new_ttl - current_ttl,
            "changed": changed,
        }

        if filter_tier and new_tier != filter_tier:
            continue

        results.append(entry)

    # Sort: changed first, then by |delta| descending
    results.sort(key=lambda x: (-int(x["changed"]), -abs(x["delta_days"])))
    return results


def main():
    parser = argparse.ArgumentParser(description="TTL Rebalance: 扫描已入库文档，评估 TTL 调整建议")
    parser.add_argument("--top", type=int, default=10, help="显示前 N 条 (default 10)")
    parser.add_argument("--tier", choices=["hot", "warm", "cold"], help="只看指定 tier")
    parser.add_argument("--json", action="store_true", dest="json_out", help="输出 JSON 报告")
    parser.add_argument("--curated-dir", default=DEFAULT_CURATED_DIR, help="curated 备份目录")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="报告输出目录")
    args = parser.parse_args()

    load_env()

    results = scan(args.curated_dir, filter_tier=args.tier)

    # Summary
    total = len(results)
    changed = sum(1 for r in results if r["changed"])
    tier_counts = {"hot": 0, "warm": 0, "cold": 0}
    for r in results:
        tier_counts[r["suggested_tier"]] = tier_counts.get(r["suggested_tier"], 0) + 1

    print(f"\n{'='*60}")
    print("  TTL Rebalance Report")
    print(f"{'='*60}")
    print(f"  Scanned: {total} docs")
    print(f"  Need rebalance: {changed}")
    print(f"  Tiers: hot={tier_counts['hot']} warm={tier_counts['warm']} cold={tier_counts['cold']}")
    print(f"{'='*60}")

    # Detail
    shown = results[: args.top]
    for r in shown:
        delta = r["delta_days"]
        sign = "+" if delta > 0 else ""
        marker = " *" if r["changed"] else ""
        print(
            f"  {r['file'][:45]:45s} "
            f"{r['current_ttl']:3d}d/{r['current_tier']:4s} → "
            f"{r['suggested_ttl']:3d}d/{r['suggested_tier']:4s} "
            f"[{sign}{delta:+d}d adopt={r['adopt_count']}]{marker}"
        )

    if total > args.top:
        print(f"  ... ({total - args.top} more, use --top {total})")

    # JSON output
    if args.json_out:
        out_dir = Path(args.data_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "ttl_rebalance_report.json"
        report = {
            "generated": datetime.date.today().isoformat(),
            "total": total,
            "changed": changed,
            "tier_counts": tier_counts,
            "items": results,
        }
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  JSON report: {out_path}")

    print()


if __name__ == "__main__":
    main()
