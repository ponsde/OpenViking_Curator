#!/usr/bin/env python3
"""Deadlock repro check for OpenViking session search.

目标：在纯净环境验证 SyncOpenViking 嵌入模式是否存在 session search 死锁，
并与 HTTP serve 模式对照，避免误判。

用法：
  python3 eval/deadlock_repro.py --mode embedded
  python3 eval/deadlock_repro.py --mode http
  python3 eval/deadlock_repro.py --mode both
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

QUERY = "OpenViking session search deadlock reproduce"


def _run_embedded(timeout_sec: int = 45, data_path: str = "/home/ponsde/OpenViking_test/data", config_file: str = "/home/ponsde/OpenViking_test/ov.conf") -> dict:
    """Run embedded SyncOpenViking session search in a subprocess with timeout."""
    tmpdir = tempfile.mkdtemp(prefix="ov_deadlock_embedded_")
    script = Path(tmpdir) / "embedded_check.py"
    script.write_text(
        """
import os, json, time
import openviking as ov
from openviking.message.part import TextPart

os.environ['OPENVIKING_CONFIG_FILE'] = os.environ.get('OV_TEST_CONFIG_FILE', '/home/ponsde/OpenViking_test/ov.conf')
client = ov.SyncOpenViking(path=os.environ.get('OV_TEST_DATA_PATH', '/home/ponsde/OpenViking_test/data'))
client.initialize()

sess_info = client.create_session()
sid = sess_info.get('session_id') if isinstance(sess_info, dict) else None
if sid:
    sess = client.session(sid)
    sess.add_message('user', [TextPart('deadlock check')])
    start = time.time()
    r = client.search('OpenViking session search deadlock reproduce', session=sess, limit=5)
    elapsed = round(time.time() - start, 2)
    n = len(getattr(r, 'resources', []) or [])
    print(json.dumps({'ok': True, 'elapsed': elapsed, 'resources': n}))
else:
    print(json.dumps({'ok': False, 'error': 'no_session_id'}))
client.close()
        """.strip(),
        encoding="utf-8",
    )

    start = time.time()
    try:
        p = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env={**os.environ, "OV_TEST_DATA_PATH": data_path, "OV_TEST_CONFIG_FILE": config_file},
        )
        elapsed = round(time.time() - start, 2)
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        return {
            "mode": "embedded",
            "timed_out": False,
            "returncode": p.returncode,
            "elapsed": elapsed,
            "stdout": out,
            "stderr": err[:500],
        }
    except subprocess.TimeoutExpired:
        return {
            "mode": "embedded",
            "timed_out": True,
            "returncode": None,
            "elapsed": timeout_sec,
            "stdout": "",
            "stderr": "timeout",
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _run_http(timeout_sec: int = 45) -> dict:
    """Run HTTP session search check."""
    start = time.time()
    try:
        # create session
        req = urllib.request.Request(
            "http://127.0.0.1:9100/api/v1/sessions",
            data=json.dumps({}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            sid = json.loads(resp.read())["result"]["session_id"]

        req = urllib.request.Request(
            "http://127.0.0.1:9100/api/v1/search/search",
            data=json.dumps({"query": QUERY, "session_id": sid, "limit": 5}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            result = json.loads(resp.read()).get("result", {})

        elapsed = round(time.time() - start, 2)
        n = len(result.get("resources", []) or [])
        return {
            "mode": "http",
            "timed_out": False,
            "returncode": 0,
            "elapsed": elapsed,
            "stdout": json.dumps({"ok": True, "resources": n}),
            "stderr": "",
        }
    except Exception as e:
        return {
            "mode": "http",
            "timed_out": False,
            "returncode": 1,
            "elapsed": round(time.time() - start, 2),
            "stdout": "",
            "stderr": str(e),
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["embedded", "http", "both"], default="both")
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--data-path", default="/home/ponsde/OpenViking_test/data")
    parser.add_argument("--config-file", default="/home/ponsde/OpenViking_test/ov.conf")
    args = parser.parse_args()

    outputs = []
    if args.mode in ("embedded", "both"):
        outputs.append(_run_embedded(timeout_sec=args.timeout, data_path=args.data_path, config_file=args.config_file))
    if args.mode in ("http", "both"):
        outputs.append(_run_http(timeout_sec=args.timeout))

    print(json.dumps({"results": outputs}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
