#!/usr/bin/env python3
import json, os, argparse
from pathlib import Path

try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False  # Windows fallback

STORE = Path(os.getenv('CURATOR_FEEDBACK_FILE', './feedback.json'))


def _locked_rw(fn):
    """Read-modify-write with exclusive file lock (Unix) or no-lock fallback (Windows)."""
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.touch(exist_ok=True)
    with open(STORE, 'r+', encoding='utf-8') as f:
        if _HAS_FCNTL:
            fcntl.flock(f, fcntl.LOCK_EX)
        try:
            raw = f.read().strip()
            data = json.loads(raw) if raw else {}
            result = fn(data)
            f.seek(0)
            f.truncate()
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
            return result
        finally:
            if _HAS_FCNTL:
                fcntl.flock(f, fcntl.LOCK_UN)


def load():
    if STORE.exists():
        with open(STORE, 'r', encoding='utf-8') as f:
            if _HAS_FCNTL:
                fcntl.flock(f, fcntl.LOCK_SH)
            try:
                raw = f.read().strip()
                return json.loads(raw) if raw else {}
            finally:
                if _HAS_FCNTL:
                    fcntl.flock(f, fcntl.LOCK_UN)
    return {}


def save(data):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    with open(STORE, 'w', encoding='utf-8') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def apply(uri: str, action: str):
    def _update(data):
        item = data.get(uri, {'up': 0, 'down': 0, 'adopt': 0})
        if action not in item:
            raise ValueError('action must be one of: up, down, adopt')
        item[action] += 1
        data[uri] = item
        return item
    return _locked_rw(_update)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description='Curator feedback store')
    p.add_argument('uri', help='resource uri')
    p.add_argument('action', choices=['up','down','adopt'])
    args = p.parse_args()
    s = apply(args.uri, args.action)
    print('ok', args.uri, s)
