# OpenViking Curator

[English](README.md) / 中文

**[OpenViking](https://github.com/volcengine/OpenViking) 的知识治理插件。** 查询 → 评估 → 外搜 → 入库 → 成长。每次提问都让你的 OV 更强。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-green.svg)](https://python.org)

## 工作流程

```
用户提问
  ↓
先查 OV
  ↓
覆盖率够？ ──够──→ 直接返回本地结果（0 次 LLM 调用）
  ↓ 不够
外部搜索（Grok / OpenAI / 自定义）
  ↓
LLM 审核质量和时效性
  ↓
通过？→ 自动入库 OV（下次直接命中）
  ↓
返回合并后的上下文（本地 + 外搜）
  ↓
你的 LLM 拿到完整上下文直接回答——一次搞定
```

## 功能列表

| 功能 | 说明 |
|------|------|
| **覆盖率门控** | 信任 OV 的 score。充足→直接返回。边缘/不足→触发外搜。 |
| **外搜 + 自动入库** | 默认 Grok（实时联网），也支持任意 OAI 兼容模型。审核通过的结果自动存入 OV，下次直接命中。 |
| **L0→L1→L2 按需加载** | 先摘要，不够才概要，最后才全文。省 token。 |
| **冲突检测** | 本地和外搜结果矛盾时自动标记。 |
| **Session 生命周期** | 记录用了哪些知识（`session.used`），自动提取长期记忆（`session.commit`）。常用知识排名更高。 |
| **查询日志 + 弱点分析** | 每次查询记录。聚类分析找出知识盲区，主动补强。 |
| **时效性扫描** | 定期扫描所有资源新鲜度。标记 fresh/aging/stale，自动刷新过期内容。 |
| **合并上下文输出** | 返回 `context` = 本地 + 外搜合并好的。你的 LLM 直接用，不需要二次查询。 |

### Curator 不做的事（OV 负责）

- 检索 / 向量搜索 → OV `find` / `search`
- 内容存储 / 索引 → OV 自己管理
- 记忆提取 / 去重 → OV `session.commit`
- 生成最终回答 → 你的 LLM

## 快速开始

### 前置条件

- Python 3.10+
- 一份 OpenViking `ov.conf`，配好 embedding + VLM 端点（[文档](https://github.com/volcengine/OpenViking)）
- 外搜 API key（推荐 Grok）和 LLM 审核 key

### 本地安装

```bash
git clone https://github.com/ponsde/OpenViking_Curator.git
cd OpenViking_Curator
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp ov.conf.example ov.conf   # 填 embedding + VLM 端点
cp .env.example .env         # 填 API key

python3 curator_query.py --status            # 健康检查
python3 curator_query.py "Docker 部署 Redis 怎么搞？"
```

### Docker（嵌入模式）

```bash
git clone https://github.com/ponsde/OpenViking_Curator.git
cd OpenViking_Curator
cp ov.conf.example ov.conf
cp .env.example .env

docker compose build
docker compose run --rm curator curator_query.py --status
docker compose run --rm curator curator_query.py "Docker 部署 Redis 怎么搞？"
```

### MCP Server

```bash
python3 mcp_server.py   # stdio JSON-RPC，兼容 Claude Desktop / mcporter / 任意 MCP 客户端
```

工具：`curator_query`、`curator_ingest`、`curator_status`

### Python 直接调用

```python
from curator.pipeline_v2 import run

result = run("GPT 自动注册脚本 Python Selenium")
print(result["context"])           # 合并好的上下文，直接给 LLM
print(result["coverage"])          # 0.0 ~ 1.0
print(result["meta"]["ingested"])  # True = 新知识已入库
```

## 输出格式

```json
{
  "context": "本地结果 + 外搜结果（合并好，直接用）",
  "coverage": 0.68,
  "conflict": {"has_conflict": false, "summary": "", "points": []},
  "meta": {
    "external_triggered": true,
    "ingested": true,
    "llm_calls": 1,
    "used_uris": ["viking://resources/..."],
    "duration": 42.5
  }
}
```

## 配置

所有配置通过 `.env`（已 git-ignored）：

| 变量 | 必填 | 说明 |
|------|------|------|
| `OPENVIKING_CONFIG_FILE` | ✅ | 你的 `ov.conf` 路径 |
| `OV_DATA_PATH` | | OV 数据目录（默认 `./data`） |
| `CURATOR_OAI_BASE` | ✅ | OAI 兼容 API 地址（用于 LLM 审核） |
| `CURATOR_OAI_KEY` | ✅ | 审核用 API key |
| `CURATOR_GROK_BASE` | | Grok 端点（默认 `http://127.0.0.1:8000/v1`） |
| `CURATOR_GROK_KEY` | ✅* | Grok API key（*使用 Grok 搜索时） |
| `OV_BASE_URL` | | 可选：连接远程 OV HTTP serve（不走嵌入模式） |

### 搜索后端（可插拔）

| 后端 | 值 | 说明 |
|------|-----|------|
| Grok | `grok`（默认） | 实时联网搜索，通过 grok2api |
| OpenAI | `oai` | 任意有联网能力的 OAI 兼容模型 |
| 自定义 | 你的名字 | 在 `search_providers.py` 注册 |

### 可调阈值

| 变量 | 默认 | 含义 |
|------|------|------|
| `CURATOR_THRESHOLD_COV_SUFFICIENT` | 0.55 | 高于此值 = 不外搜 |
| `CURATOR_THRESHOLD_COV_MARGINAL` | 0.45 | 高于此值 = 边缘（仍会外搜） |
| `CURATOR_THRESHOLD_COV_LOW` | 0.35 | 低于此值 = 一定外搜 |

## 维护脚本

| 脚本 | 功能 |
|------|------|
| `scripts/analyze_weak.py` | 从查询日志聚类分析弱覆盖话题 |
| `scripts/strengthen.py` | 对 top N 弱话题主动跑 pipeline 补强 |
| `scripts/freshness_scan.py` | 扫描所有资源新鲜度，`--act` 自动刷新过期内容 |

## 项目结构

```
curator/
  pipeline_v2.py       # 主流程（4 步，返回结构化数据）
  session_manager.py   # 双模式 OV 客户端（嵌入 / HTTP）
  retrieval_v2.py      # L0→L1→L2 加载 + 覆盖率评估
  search.py            # 外搜 + 交叉验证
  review.py            # LLM 审核 + 入库 + 冲突检测
  router.py            # 轻量规则路由
  config.py            # 全部配置（env 可覆盖）
  freshness.py         # 时间衰减评分
  dedup.py             # 资源重复扫描
  legacy/              # 归档的 v1 模块
curator_query.py       # CLI 入口
mcp_server.py          # MCP 服务器（stdio）
search_providers.py    # 可插拔搜索后端
scripts/               # 维护脚本
tests/                 # 单元测试（77 passing）
```

## 测试

```bash
python -m pytest tests/ -v
```

## Roadmap

- [ ] 修复 `active_count` URI 格式匹配（短 URI → 完整 URI）
- [ ] LLM 智能合并相似资源
- [ ] 定时任务：`analyze_weak.py` + `freshness_scan.py` 自动运行
- [ ] 自优化：效果追踪 + 阈值自动调优
- [ ] 批量导入历史笔记到 OV
- [ ] OV 知识库清理（去重时间戳命名条目）
- [ ] 每周知识健康报告

## License

[MIT](LICENSE)
