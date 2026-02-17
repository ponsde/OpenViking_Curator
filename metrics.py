#!/usr/bin/env python3
import json, time
from pathlib import Path

class Metrics:
    def __init__(self, path='output/eval_report.jsonl'):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = {
            'started_at': time.time(),
            'steps': [],
            'flags': {},
            'scores': {}
        }

    def step(self, name, ok=True, extra=None):
        self.data['steps'].append({
            'ts': time.time(),
            'name': name,
            'ok': ok,
            'extra': extra or {}
        })

    def flag(self, key, val):
        self.data['flags'][key] = val

    def score(self, key, val):
        self.data['scores'][key] = val

    def finalize(self):
        self.data['finished_at'] = time.time()
        self.data['duration_sec'] = round(self.data['finished_at'] - self.data['started_at'], 2)
        with self.path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(self.data, ensure_ascii=False) + '\n')
        return self.data
