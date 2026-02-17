#!/usr/bin/env python3
import json, os, argparse
from pathlib import Path

STORE = Path(os.getenv('CURATOR_FEEDBACK_FILE', './feedback.json'))


def load():
    if STORE.exists():
        return json.loads(STORE.read_text(encoding='utf-8'))
    return {}


def save(data):
    STORE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def apply(uri: str, action: str):
    data = load()
    item = data.get(uri, {'up': 0, 'down': 0, 'adopt': 0})
    if action not in item:
        raise ValueError('action must be one of: up, down, adopt')
    item[action] += 1
    data[uri] = item
    save(data)
    return item


if __name__ == '__main__':
    p = argparse.ArgumentParser(description='Curator feedback store')
    p.add_argument('uri', help='resource uri')
    p.add_argument('action', choices=['up','down','adopt'])
    args = p.parse_args()
    s = apply(args.uri, args.action)
    print('ok', args.uri, s)
