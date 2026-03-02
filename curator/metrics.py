#!/usr/bin/env python3
import json
import time
from pathlib import Path


class Metrics:
    def __init__(self, path="output/eval_report.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = {"started_at": time.time(), "steps": [], "flags": {}, "scores": {}}

    def step(self, name, ok=True, extra=None):
        now = time.time()
        steps = self.data["steps"]
        prev_ts = steps[-1]["ts"] if steps else self.data["started_at"]
        elapsed_ms = round((now - prev_ts) * 1000, 2)
        merged_extra = {**(extra or {}), "elapsed_ms": elapsed_ms}
        self.data["steps"].append({"ts": now, "name": name, "ok": ok, "extra": merged_extra})

    def flag(self, key, val):
        self.data["flags"][key] = val

    def score(self, key, val):
        self.data["scores"][key] = val

    def finalize(self):
        self.data["finished_at"] = time.time()
        self.data["duration_sec"] = round(self.data["finished_at"] - self.data["started_at"], 2)
        from .file_lock import locked_append

        locked_append(self.path, json.dumps(self.data, ensure_ascii=False) + "\n")
        return self.data
