#!/usr/bin/env python3
"""
v0.4 maintenance utilities:
- feedback decay (older signals gradually weaken)
- stale case report (files not updated for N days)
"""
import json
import time
import argparse
from pathlib import Path


def decay_feedback(path: Path, factor: float = 0.9):
    if not path.exists():
        return {"updated": 0, "total": 0}
    data = json.loads(path.read_text(encoding="utf-8"))
    updated = 0
    for uri, item in data.items():
        if not isinstance(item, dict):
            continue
        for k in ("up", "down", "adopt"):
            if k in item and isinstance(item[k], (int, float)):
                old = item[k]
                item[k] = round(old * factor, 3)
        updated += 1
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"updated": updated, "total": len(data)}


def stale_cases(case_dir: Path, stale_days: int = 30):
    if not case_dir.exists():
        return []
    now = time.time()
    threshold = stale_days * 86400
    stale = []
    for p in case_dir.glob("*.md"):
        age = now - p.stat().st_mtime
        if age > threshold:
            stale.append({"file": str(p), "age_days": round(age / 86400, 1)})
    stale.sort(key=lambda x: x["age_days"], reverse=True)
    return stale


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--feedback-file", default="feedback.json")
    ap.add_argument("--case-dir", default="cases")
    ap.add_argument("--decay-factor", type=float, default=0.9)
    ap.add_argument("--stale-days", type=int, default=30)
    args = ap.parse_args()

    fb_path = Path(args.feedback_file)
    case_dir = Path(args.case_dir)

    d = decay_feedback(fb_path, args.decay_factor)
    s = stale_cases(case_dir, args.stale_days)

    print(json.dumps({
        "feedback_decay": d,
        "stale_case_count": len(s),
        "stale_cases_top10": s[:10]
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
