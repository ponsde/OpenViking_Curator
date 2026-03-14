# AI Context

## 项目简介

OpenViking Curator — OpenViking (OV) 知识库的**治理层插件**。不生成答案，返回结构化上下文供调用方 LLM 使用。负责：外搜→审核→冲突检测→入库，以及周期性治理维护。

## 技术栈

- **语言**: Python 3.10+
- **依赖**: Pydantic (数据模型), httpx (HTTP), OpenViking SDK (向量检索)
- **LLM**: OAI-compatible API (`config.chat()`, 带 retry)
- **测试**: pytest, monkeypatch, InMemoryBackend (零外部依赖)
- **搜索**: 可插拔 provider chain (grok/duckduckgo/tavily)

## 目录结构

```
OpenViking_Curator/
├── curator/                 ← 核心包
│   ├── pipeline_v2.py       ← 主编排器 (Query→Route→Retrieve→Search→Judge→Ingest)
│   ├── router.py            ← 规则路由
│   ├── retrieval_v2.py      ← L0→L1→L2 分级检索 + feedback reranking
│   ├── search.py            ← 外搜触发逻辑
│   ├── search_providers.py  ← 可插拔搜索 provider
│   ├── review.py            ← Judge (LLM 审核 + 冲突检测) + ingest
│   ├── backend.py           ← KnowledgeBackend ABC
│   ├── backend_ov.py        ← OpenViking 生产后端
│   ├── backend_memory.py    ← 内存后端 (测试用)
│   ├── config.py            ← env() 配置 + chat() LLM 客户端
│   ├── governance.py        ← 周治理 5 阶段
│   ├── governance_cli.py    ← 治理 CLI
│   ├── governance_report.py ← ASCII/JSON/HTML 报告
│   ├── interest_analyzer.py ← 用户兴趣提取
│   ├── feedback_store.py    ← up/down/adopt 反馈
│   ├── metrics.py           ← 指标收集
│   └── settings.py          ← Pydantic Settings
├── curator_query.py         ← CLI 入口
├── mcp_server.py            ← MCP server (stdio JSON-RPC)
├── scripts/                 ← 维护脚本 (analyze_weak, strengthen, freshness_scan...)
├── tests/                   ← 全部测试 (InMemoryBackend, monkeypatch LLM)
├── schemas/                 ← JSON Schema
├── eval/                    ← 评估用例
└── pyproject.toml           ← 包配置
```

## 架构概要

```
curator_query.py / mcp_server.py        (入口)
        ↓
pipeline_v2.py                           (编排器)
        ↓
┌───────────────┬────────────────┬────────────────┐
│ router.py     │ retrieval_v2.py│ search.py      │
│ (规则路由)     │ (L0→L1→L2)    │ + providers    │
└───────┬───────┴───────┬────────┴───────┬────────┘
        └───────────────┼────────────────┘
                        ↓
                  review.py              (Judge + Ingest + Conflict)
                        ↓
                  backend.py (ABC)       (KnowledgeBackend 接口)
                   ├── backend_ov.py     (生产)
                   └── backend_memory.py (测试)

governance.py → interest_analyzer.py + governance_report.py + governance_cli.py
```

**Pipeline 流程**: Query → Route → Retrieve → Coverage 评估 → [外搜] → [Judge+Conflict] → [Ingest] → Return

**LLM 调用预算**: coverage 足够 = 0 次; 外搜触发 = 1 次; 需要 freshness = 2 次

**分级检索**: L0 (~100 token) → L1 (~2K token, top 3) → L2 (full, max 2)

## 约定

- **不可变**: pipeline 各阶段返回新 dict，不修改输入
- **无魔数**: 所有阈值通过 `config.env()` 配置
- **后端无关**: pipeline/review 代码不直接 import openviking，通过 KnowledgeBackend 接口
- **返回数据不返回答案**: 返回 `context_text` + metadata，不生成 LLM 答案
- **中英双语**: 代码注释英文，用户面字符串双语
- **测试零依赖**: 全部使用 InMemoryBackend + monkeypatch，无网络无 OV

## 注意事项

- `ASYNC_INGEST` 是 Pydantic Settings 模块级常量，env 运行时改不了，测试必须 `patch.multiple("curator.pipeline_v2", ASYNC_INGEST=True/False)`
- OV SDK 已升级到 v0.2.1，使用 `session_exists()` 正式 API
- 实时 pipeline (`curator_query.py`) vs 周治理 (`governance_cli run`) 是两条独立路径，别搞混
- 必需环境变量: `OPENVIKING_CONFIG_FILE`, `CURATOR_OAI_BASE`, `CURATOR_OAI_KEY`
