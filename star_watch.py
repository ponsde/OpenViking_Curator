#!/usr/bin/env python3
import json
import time
import subprocess
from pathlib import Path

REPO = "ponsde/OpenViking_Curator"
STATE = Path("/home/ponsde/OpenViking_Curator/.star_state.json")


def get_stars():
    out = subprocess.check_output([
        "gh", "api", f"repos/{REPO}", "--jq", ".stargazers_count"
    ], text=True).strip()
    return int(out)


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"stars": None, "updated_at": None}


def save_state(stars):
    STATE.write_text(json.dumps({
        "stars": stars,
        "updated_at": int(time.time())
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    cur = get_stars()
    old = load_state().get("stars")
    if old is None:
        save_state(cur)
        print(f"INIT stars={cur}")
        return
    if cur != old:
        delta = cur - old
        save_state(cur)
        print(f"CHANGED old={old} new={cur} delta={delta}")
    else:
        print(f"NO_CHANGE stars={cur}")


if __name__ == "__main__":
    main()
