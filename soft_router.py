#!/usr/bin/env python3
import os
import re
import subprocess
import sys

KNOWLEDGE_PATTERNS = [
    r"是什么", r"区别", r"原理", r"架构", r"总结", r"对比", r"文档", r"资料",
    r"what is", r"difference", r"architecture", r"summary", r"compare", r"docs",
]


def is_knowledge_query(q: str) -> bool:
    t = (q or "").lower()
    for p in KNOWLEDGE_PATTERNS:
        if re.search(p, t, re.IGNORECASE):
            return True
    return False


def run_curator(q: str) -> int:
    py = os.getenv("CURATOR_PY", "/home/ponsde/OpenViking_test/.venv/bin/python")
    script = os.getenv("CURATOR_SCRIPT", "/home/ponsde/OpenViking_Curator/curator_v0.py")
    cmd = [py, script, q]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if p.returncode == 0:
        print(p.stdout)
    else:
        print("[soft-router] curator_error_suppressed")
    return p.returncode


def main():
    q = " ".join(sys.argv[1:]).strip()
    if not q:
        print("usage: soft_router.py <query>")
        sys.exit(2)

    if is_knowledge_query(q):
        print("[soft-router] route=curator")
        rc = run_curator(q)
        if rc == 0:
            sys.exit(0)
        print("[soft-router] curator_failed_fallback=default")
        print("NO_ROUTE")
        sys.exit(0)
    else:
        print("[soft-router] route=default")
        print("NO_ROUTE")


if __name__ == "__main__":
    main()
