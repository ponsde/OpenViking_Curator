# Contributing to OpenViking Curator

Thank you for your interest in contributing. This guide covers everything you need
to get started: environment setup, testing, code style, architecture pointers, and
the step-by-step recipes for the two most common extension points.

---

## Table of Contents

1. [Dev Environment Setup](#1-dev-environment-setup)
2. [Running Tests](#2-running-tests)
3. [Code Style](#3-code-style)
4. [Architecture Overview](#4-architecture-overview)
5. [Adding a New Search Provider](#5-adding-a-new-search-provider)
6. [Adding a New Backend](#6-adding-a-new-backend)
7. [PR Process](#7-pr-process)
8. [Security](#8-security)

---

## 1. Dev Environment Setup

### Prerequisites

- Python 3.10+
- Git
- A working `.env` file (see step 4)

### Steps

```bash
# Clone the repository
git clone https://github.com/ponsde/OpenViking_Curator.git
cd OpenViking_Curator

# Install the package in editable mode with dev dependencies
pip install -e ".[dev]"

# (Alternative) Use uv for faster installs
# curl -LsSf https://astral.sh/uv/install.sh | sh
# uv sync --extra dev

# Copy the example env file and fill in your API keys
cp .env.example .env
$EDITOR .env   # set CURATOR_OAI_BASE, CURATOR_OAI_KEY, etc.

# Install pre-commit hooks (runs ruff + mypy on every commit)
pre-commit install
```

The three required environment variables are:

| Variable | Purpose |
|---|---|
| `OPENVIKING_CONFIG_FILE` | Path to the OpenViking config file |
| `CURATOR_OAI_BASE` | OAI-compatible LLM base URL (e.g. `https://api.openai.com/v1`) |
| `CURATOR_OAI_KEY` | API key for the LLM endpoint |

All other variables have sensible defaults. See `.env.example` and `curator/config.py`
for the full list.

---

## 2. Running Tests

```bash
# Run the full test suite (verbose)
python -m pytest tests/ -v

# Run a single test file
python -m pytest tests/test_core.py -v

# Run a single test by name
python -m pytest tests/test_core.py -k "test_route_scope" -v

# Run with coverage report (70% gate enforced in CI)
python -m pytest tests/ --cov=curator --cov-report=term-missing

# Alternatively with uv
uv run pytest tests/ -v
```

The coverage gate is **70%** (enforced in CI via `pytest-cov`). New code should aim
for 80%+ and must not drop the overall project coverage below the gate.

`curator/legacy/` is excluded from coverage measurement — do not add new code there.

### Test Isolation

All tests use `InMemoryBackend` — no OpenViking installation, no network calls needed.

```python
# Common test pattern / 常见测试模式
from curator.backend_memory import InMemoryBackend

def test_something(monkeypatch):
    backend = InMemoryBackend()
    # Ingest test data directly / 直接注入测试数据
    backend.ingest("content", title="test", metadata={})
    # Monkeypatch all LLM calls / 打桩替换所有 LLM 调用
    monkeypatch.setattr("curator.config.chat", lambda *a, **kw: mock_response)
    result = run("query", backend=backend)
```

Key conventions:
- Patch `curator.config.chat` to intercept every LLM call.
- Patch `curator.pipeline_v2.ASYNC_INGEST` via `unittest.mock.patch.multiple` — it
  is a Pydantic Settings constant loaded at module import time, so `monkeypatch.setenv`
  does **not** work at runtime.
- Never make real network calls in tests.

---

## 3. Code Style

Style is enforced automatically by pre-commit hooks on every commit.

### Tools

| Tool | Purpose | Config |
|---|---|---|
| `ruff` | Lint (E, F, W, I rules) + format | `[tool.ruff]` in `pyproject.toml` |
| `mypy` | Static type checking | `[tool.mypy]` in `pyproject.toml` |

```bash
# Run all checks manually
pre-commit run --all-files

# Lint only
ruff check curator/

# Format only
ruff format curator/

# Type check only
mypy curator/
```

Line length is **120 characters** (`E501` is not enforced — the formatter handles it).
`curator/legacy/` is excluded from both ruff and mypy.

### General conventions

- **Immutability**: pipeline stages return new dicts, never mutate inputs.
- **No magic numbers**: all thresholds belong in `config.py` via `env()`.
- **Comments**: English in code; user-facing strings may be bilingual (Chinese + English).
- **Return data, not answers**: Curator returns `context_text` + metadata, never
  LLM-generated answers.
- **Backend agnostic**: never `import openviking` directly in pipeline or review code —
  always go through `KnowledgeBackend`.

---

## 4. Architecture Overview

Curator is the **governance layer** for OpenViking. It does not generate answers — it
returns structured context for the caller's LLM.

```
Query → Route → Retrieve → Assess Coverage → [External Search] → [Judge+Conflict] → [Ingest] → Return
```

For the full architecture diagram, module dependency map, retrieval tiers (L0/L1/L2),
feedback reranking formula, and config reference, see [CLAUDE.md](./CLAUDE.md).

Key abstractions at a glance:

- **`KnowledgeBackend` (ABC)** — all storage goes through this interface.
- **`pipeline_v2.run()`** — the main entry point; orchestrates everything.
- **`search_providers.py`** — pluggable external search with fallback chain.
- **`review.py`** — single LLM call for judge + conflict detection (`JudgeResult`).
- **`retrieval_v2.py`** — L0→L1→L2 on-demand loading + feedback reranking.

---

## 5. Adding a New Search Provider

Each external search provider is a single function registered in the `_PROVIDERS`
dict inside `curator/search_providers.py`. Here is the full recipe.

### Step 1 — Write the provider function

```python
# curator/search_providers.py

# Provider function signature: (query: str, scope: dict) -> list[WebSearchResult] | str
# Return list[WebSearchResult] when you can parse structured results.
# Return str as a fallback (plain text).
def _search_myprovider(query: str, scope: dict) -> list[WebSearchResult] | str:
    """Search via MyProvider and return structured results.
    通过 MyProvider 搜索并返回结构化结果。
    """
    try:
        from myprovider_sdk import Client  # optional dependency
    except ImportError as e:
        raise ImportError("myprovider-sdk not installed: pip install myprovider-sdk") from e

    api_key = env("CURATOR_MYPROVIDER_KEY", "")
    if not api_key:
        raise RuntimeError("CURATOR_MYPROVIDER_KEY not configured; skipping MyProvider")

    client = Client(api_key=api_key)
    raw = client.search(query, max_results=5)

    out: list[WebSearchResult] = []
    for item in raw:
        out.append(
            WebSearchResult(
                title=str(item.get("title", "")),
                url=str(item.get("url", "")),
                snippet=str(item.get("body", "")),
            )
        )
    return out
```

Rules:
- Use `env()` (from `curator.config`) to read the API key — never hardcode it.
- Raise `ImportError` if the optional SDK is not installed.
- Raise `RuntimeError` if a required key is missing; the caller will skip this provider
  and try the next one in the chain.
- Return `list[WebSearchResult]` when possible; raw `str` only as a fallback.
- LLM-backed providers (like `grok`/`oai`) should set `source_type = "llm_generated"`
  on each result so downstream code can apply appropriate scepticism.

### Step 2 — Register the provider

```python
# curator/search_providers.py  — inside the _PROVIDERS dict
_PROVIDERS = {
    "grok": "_search_grok",
    "oai": "_search_oai",
    "duckduckgo": "_search_duckduckgo",
    "tavily": "_search_tavily",
    "myprovider": "_search_myprovider",   # <-- add this line
}
```

The value is the **string name** of the function attribute on the module. This
indirection is intentional: it allows `unittest.mock.patch.object` to be picked up
at call time.

### Step 3 — Document the env var

Add the new key to `.env.example` with a comment explaining where to get it.

### Step 4 — Write tests

```python
# tests/test_search_providers.py

def test_myprovider_returns_results(monkeypatch):
    """MyProvider should return structured WebSearchResult list."""
    import curator.search_providers as sp
    monkeypatch.setattr(sp, "_search_myprovider", lambda q, s: [
        sp.WebSearchResult(title="T", url="https://example.com", snippet="S"),
    ])
    result = sp.search("test query", {}, provider="myprovider")
    assert "example.com" in result

def test_myprovider_missing_key(monkeypatch):
    """Missing API key should raise RuntimeError."""
    monkeypatch.setenv("CURATOR_MYPROVIDER_KEY", "")
    import curator.search_providers as sp
    import importlib
    importlib.reload(sp)  # reload so env var is picked up
    with pytest.raises(RuntimeError, match="not configured"):
        sp._search_myprovider("query", {})
```

### Step 5 — Activate

Set the env var to include your new provider in the chain:

```bash
# .env or shell
CURATOR_SEARCH_PROVIDERS=grok,myprovider,duckduckgo
```

Providers are tried left-to-right in fallback mode; all run in parallel in concurrent
mode (`CURATOR_SEARCH_CONCURRENT=1`).

---

## 6. Adding a New Backend

All storage is accessed through `KnowledgeBackend` (abstract base class in
`curator/backend.py`). To plug Curator into a different knowledge store (Milvus,
Qdrant, Chroma, pgvector, etc.), implement this interface.

### Step 1 — Create the implementation file

```bash
touch curator/backend_mystore.py
```

### Step 2 — Implement the required methods

```python
# curator/backend_mystore.py
"""MyStore backend for Curator.
将 Curator 接入 MyStore 知识库的后端实现。
"""

from .backend import KnowledgeBackend, SearchResponse, SearchResult


class MyStoreBackend(KnowledgeBackend):
    """KnowledgeBackend implementation for MyStore.

    Args:
        host: MyStore server host.
        api_key: API key for authentication.
    """

    def __init__(self, host: str, api_key: str):
        # Store config, initialise client — no network calls here yet
        # 存储配置，初始化客户端——暂不发起网络调用
        self._host = host
        self._client = MyStoreClient(host=host, api_key=api_key)

    # ── Required: health ──

    def health(self) -> bool:
        """Return True if the backend is reachable."""
        try:
            return self._client.ping()
        except Exception:
            return False

    # ── Required: find (fast vector search, no LLM) ──

    def find(self, query: str, limit: int = 10) -> SearchResponse:
        raw = self._client.vector_search(query, limit=limit)
        results = [
            SearchResult(uri=r["id"], abstract=r.get("summary", ""), score=r["score"])
            for r in raw
        ]
        return SearchResponse(results=results, total=len(results))

    # ── Required: search (full search, may use LLM intent) ──
    # If your backend has no LLM search, just delegate to find().
    # 如果后端没有 LLM 搜索，直接委托给 find() 即可。

    def search(self, query: str, limit: int = 10, session_id: str | None = None) -> SearchResponse:
        return self.find(query, limit=limit)

    # ── Required: read (full content) ──

    def read(self, uri: str) -> str:
        return self._client.get_content(uri)

    # ── Required: ingest ──

    def ingest(self, content: str, title: str = "", metadata: dict | None = None) -> str:
        result = self._client.store(content=content, title=title, metadata=metadata or {})
        return result["id"]

    # ── Optional: tiered loading (L0/L1) ──
    # Override these for faster token-efficient retrieval.
    # 覆盖这两个方法可启用分层加载，减少 token 消耗。

    def abstract(self, uri: str) -> str:
        return self._client.get_abstract(uri)  # ~100 tokens

    def overview(self, uri: str) -> str:
        return self._client.get_overview(uri)  # ~2k tokens

    # ── Optional: advertise capabilities ──

    @property
    def name(self) -> str:
        return "MyStoreBackend"

    @property
    def supports_tiered_loading(self) -> bool:
        # Set to True only if abstract() and overview() are implemented.
        # 仅在实现了 abstract() 和 overview() 时才设为 True。
        return True
```

### Step 3 — Wire it into the pipeline

```python
from curator.backend_mystore import MyStoreBackend
from curator.pipeline_v2 import run

backend = MyStoreBackend(host="http://localhost:8080", api_key="...")
result = run("How to deploy Redis?", backend=backend)
```

Or inject it into `CuratorPipeline` for reuse across requests:

```python
from curator.pipeline_v2 import CuratorPipeline

pipeline = CuratorPipeline(backend=backend)
result = pipeline.run("How to deploy Redis?")
```

### Step 4 — Write tests

Use the same `InMemoryBackend` pattern as existing tests for unit tests. For
integration tests, add a new file `tests/test_backend_mystore.py` and guard it with
`pytest.mark.integration` so it does not run in CI without the real service.

### Required vs optional methods summary

| Method | Required | Notes |
|---|---|---|
| `health()` | Yes | Returns `bool` |
| `find()` | Yes | Fast vector search |
| `search()` | Yes | May delegate to `find()` |
| `read()` | Yes | Full content |
| `ingest()` | Yes | Returns URI string |
| `abstract()` | No | L0 (~100 tokens) |
| `overview()` | No | L1 (~2k tokens) |
| `wait_indexed()` | No | No-op for sync indexing |
| `delete()` | No | Returns `False` by default |
| `list_resources()` | No | Returns `[]` by default |
| Session methods | No | No-op by default |

---

## 7. PR Process

### Branch naming

```
feat/<short-description>
fix/<short-description>
refactor/<short-description>
docs/<short-description>
test/<short-description>
chore/<short-description>
```

Examples: `feat/bing-search-provider`, `fix/coverage-clamp-off-by-one`

### Commit message format

[Conventional Commits](https://www.conventionalcommits.org/):

```
<type>: <description>

<optional body>
```

Types: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `perf`, `ci`

Examples:
```
feat: add Bing search provider
fix: coverage score clamp to [0, 1]
test: add 8 settings validation tests
refactor: extract assess_coverage magic numbers to constants
```

### Before opening a PR

- [ ] All tests pass: `python -m pytest tests/ -v`
- [ ] Pre-commit hooks pass: `pre-commit run --all-files`
- [ ] New code has tests (aim for 80%+ coverage on new files)
- [ ] README or CLAUDE.md updated if the change affects public API or architecture
- [ ] No hardcoded secrets (see [Security](#8-security))

### PR description

Include:
1. What changed and why
2. How to test it manually (if applicable)
3. Any breaking changes

---

## 8. Security

### No hardcoded secrets

Never commit API keys, passwords, or tokens to the repository. All secrets must
go through environment variables loaded via `curator/config.py`'s `env()` function.

```python
# Wrong — never do this / 错误示例，禁止
API_KEY = "sk-abc123..."

# Correct — always use env() / 正确示例
from curator.config import env
API_KEY = env("CURATOR_MY_KEY", "")
```

The pre-commit hooks do not automatically scan for secrets — use a tool like
[`gitleaks`](https://github.com/gitleaks/gitleaks) or review diffs carefully before
committing.

### Mandatory checks before any commit

- [ ] No hardcoded API keys, passwords, or tokens
- [ ] All user input is validated before processing
- [ ] New HTTP endpoints (if any) have rate limiting
- [ ] Error messages do not leak internal paths or secrets

### Reporting a vulnerability

Do not open a public GitHub issue for security vulnerabilities. Instead, report them
privately via the [GitHub Security Advisory](https://github.com/ponsde/OpenViking_Curator/security/advisories/new)
feature, or by opening a confidential issue and marking it with the `security` label.

Include:
- A description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

We aim to acknowledge reports within 48 hours and provide a fix within 7 days for
critical issues.
