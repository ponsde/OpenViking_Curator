# OpenViking Curator

[English](README.md) / 中文

**[OpenViking](https://github.com/volcengine/OpenViking) 的主动知识治理层。** 不只是检索——判断、验证、积累。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-green.svg)](https://python.org)
[![Tests: 22 passing](https://img.shields.io/badge/Tests-22%20passing-brightgreen.svg)](tests/)

## 这是什么？

传统 RAG：放数据进去、取数据出来。Curator 加了治理层：

| 功能 | 说明 |
|------|------|
| **覆盖率门控** | 评估本地检索质量，只在不够时触发外搜 |
| **可插拔外部搜索** | Grok、OpenAI 或自定义后端——换个环境变量就行 |
| **AI 审核入库** | 外搜结果经过质量/时效审核才入库 |
| **交叉验证** | 易变声明自动对官方源交叉验证 |
| **冲突检测** | 识别本地与外部信息的矛盾 |
| **时效追踪** | TTL 元数据、过期扫描、过时知识检测 |
| **反馈闭环** | 用户反馈影响未来检索排序 |
| **案例沉淀** | 自动保存问答为可复用经验 |

## 快速开始

### 方式 A：本地安装

```bash
git clone https://github.com/ponsde/OpenViking_Curator.git
cd OpenViking_Curator
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填入你的 API key
```

### 方式 B：Docker

```bash
git clone https://github.com/ponsde/OpenViking_Curator.git
cd OpenViking_Curator
cp .env.example .env   # 填入你的 API key
docker compose build
docker compose run curator "OpenViking 是什么？"
```

### 试一下

```bash
# 健康检查
python3 curator_query.py --status

# 提问
python3 curator_query.py "Redis 和 Memcached 高并发下怎么选？"
```

## 架构

```
查询 → 门控（纯规则，不调 LLM）→ 路由 → 本地检索（OpenViking）
                                               ↓
                                     覆盖率 + 核心词检查
                                          ↙         ↘
                                    足够？       外部搜索（可插拔）
                                      ↓                ↓
                                    回答       交叉验证 → 审核 → 入库？
                                      ↓                         ↓
                                  来源透明度                   回答
                                      ↓                         ↓
                                  案例沉淀                  冲突检测
```

## 搜索后端

外搜后端**可插拔**。在 `.env` 里设 `CURATOR_SEARCH_PROVIDER`：

| 后端 | 值 | 说明 |
|------|-----|------|
| Grok | `grok`（默认） | 通过 grok2api 或兼容端点 |
| OpenAI | `oai` | 任意有联网能力的 OAI 兼容模型 |
| 自定义 | 你的名字 | 在 `search_providers.py` 注册 |

```python
# search_providers.py 里加你自己的
def bing_search(query, scope, **kwargs) -> str:
    ...
    return result_text

PROVIDERS["bing"] = bing_search
```

## 配置

所有配置通过环境变量（`.env` 文件，已 git-ignored）：

| 变量 | 必填 | 说明 |
|------|------|------|
| `CURATOR_OAI_BASE` | ✅ | OAI 兼容 API 地址 |
| `CURATOR_OAI_KEY` | ✅ | API key |
| `CURATOR_GROK_KEY` | ✅* | Grok API key（*仅使用 Grok 后端时） |
| `CURATOR_SEARCH_PROVIDER` | | 搜索后端：`grok`（默认）、`oai`、自定义 |

### 可调阈值

所有阈值支持 env 覆盖：

| 变量 | 默认 | 含义 |
|------|------|------|
| `CURATOR_THRESHOLD_LOW_COV` | 0.45 | 低于此值触发外搜 |
| `CURATOR_THRESHOLD_CORE_COV` | 0.4 | 核心词覆盖 ≤ 此值触发外搜 |
| `CURATOR_THRESHOLD_LOW_TRUST` | 5.4 | 低于此值触发质量补充搜索 |

## 项目结构

```
curator_v0.py          # 核心 8 步管线
curator_query.py       # CLI 入口（--help, --status, 查询）
search_providers.py    # 可插拔搜索后端
mcp_server.py          # MCP 服务器（stdio JSON-RPC，3 个工具）
feedback_store.py      # 线程安全的反馈存储
dedup.py               # AI 去重（scan/clean/merge）
batch_ingest.py        # 批量入库（冷启动用）
eval_batch.py          # 基准测试（10 题）
freshness_rescan.py    # URL 可达性 + TTL 过期扫描
Dockerfile             # 容器构建
docker-compose.yml     # 一键 Docker 启动
tests/test_core.py     # 22 个单元测试
```

## 测试

```bash
python -m pytest tests/ -v   # 22 个测试，全部离线（不调外部 API）
```

## 路线图

见 [ROADMAP.md](ROADMAP.md)。当前版本 **v0.8**。

## 贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## License

[MIT](LICENSE)
