# OpenViking Curator

[English](README.md) / 中文

**[OpenViking](https://github.com/volcengine/OpenViking) 的知识治理层。** 不只是检索——判断、验证、积累。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-green.svg)](https://python.org)

## 这是什么？

传统 RAG：放数据进去、取数据出来。Curator 加了治理层：

| 功能 | 说明 |
|------|------|
| **覆盖率门控** | 评估本地检索质量（信任 OV 的 score），只在不够时触发外搜 |
| **可插拔外部搜索** | Grok、OpenAI 或自定义后端——换个环境变量就行 |
| **AI 审核入库** | 外搜结果经过质量/时效审核才存回 OV |
| **交叉验证** | 标记外搜结果中的易变/高风险声明 |
| **冲突检测** | 识别本地与外部信息的矛盾 |
| **案例沉淀** | 自动保存问答为可复用经验 |

### Curator 不做的事（由 OV 负责）

- ❌ 检索 / 语义搜索（OV `find` / `search`）
- ❌ L0/L1/L2 内容加载（OV `abstract` / `overview` / `read`）
- ❌ 记忆提取（OV `session.commit`）
- ❌ 信任/时效评分（OV `active_count` + score 排序）
- ❌ 去重（OV 自身管理知识库）
- ❌ 生成最终回答（调用方的 LLM 做）

## 快速开始

### 方式 A：本地安装

```bash
git clone https://github.com/ponsde/OpenViking_Curator.git
cd OpenViking_Curator
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填入你的 API key
```

### 方式 B：Docker（嵌入模式）

```bash
git clone https://github.com/ponsde/OpenViking_Curator.git
cd OpenViking_Curator
cp ov.conf.example ov.conf   # 填 embedding + VLM 配置
cp .env.example .env         # 填 curator 的 key

docker compose build
docker compose run --rm curator curator_query.py --status
docker compose run --rm curator curator_query.py "OpenViking 是什么？"
```

### 试一下

```bash
python3 curator_query.py --status
python3 curator_query.py "Redis 和 Memcached 高并发下怎么选？"
```

## 架构

```
查询 → 门控（纯规则） → OV Search（session + VLM 意图分析）
                                    ↓
                          L0→L1→L2 严格按需加载
                          + 覆盖率评估（信任 OV score）
                                 ↙         ↘
                           足够？       外部搜索（可插拔）
                               ↓                ↓
                          直接返回      交叉验证（标记风险）
                                                 ↓
                                          审核 → 入库回 OV？
                                                 ↓
                                          冲突检测
                                                 ↓
                                        结构化输出
                                        { context_text,
                                          external_text,
                                          coverage,
                                          conflict, meta }
```

**设计原则：**
- 返回结构化数据，不生成回答
- 信任 OV 的 score，不重新打分
- 严格按需加载：先 L0，不够才 L1，还不够才 L2
- 外搜是补充，不是替代

## 搜索后端

外搜后端**可插拔**。在 `.env` 里设 `CURATOR_SEARCH_PROVIDER`：

| 后端 | 值 | 说明 |
|------|-----|------|
| Grok | `grok`（默认） | 通过 grok2api 或兼容端点 |
| OpenAI | `oai` | 任意有联网能力的 OAI 兼容模型 |
| 自定义 | 你的名字 | 在 `search_providers.py` 注册 |

## 配置

所有配置通过环境变量（`.env` 文件，已 git-ignored）：

| 变量 | 必填 | 说明 |
|------|------|------|
| `CURATOR_OAI_BASE` | ✅ | OAI 兼容 API 地址 |
| `CURATOR_OAI_KEY` | ✅ | API key（用于审核/验证） |
| `CURATOR_GROK_KEY` | ✅* | Grok API key（*仅使用 Grok 后端时） |
| `CURATOR_SEARCH_PROVIDER` | | 搜索后端：`grok`（默认）、`oai`、自定义 |
| `CURATOR_JUDGE_MODELS` | | 审核/验证模型 fallback 链 |
| `OPENVIKING_CONFIG_FILE` | | OpenViking ov.conf 路径（嵌入模式） |
| `OV_BASE_URL` | | 可选：使用 OV HTTP serve（不走嵌入） |


## 项目结构

```
curator/               # 核心包（治理层）
  config.py            # 环境变量、阈值、HTTP 客户端
  router.py            # 轻量路由（领域 + 时效性判断）
  retrieval_v2.py      # 严格按需 L0→L1→L2 加载 + 覆盖率评估
  session_manager.py   # 双模式 OV client（嵌入/HTTP）+ 持久 session 生命周期
  search.py            # 外搜 + 交叉验证（风险标记）
  review.py            # 审核入库 + 冲突检测
  pipeline_v2.py       # 主流程（5 步，返回结构化数据）
  legacy/              # 归档的 v1 模块（answer, feedback, dedup, pipeline, retrieval）
curator_query.py       # CLI 入口
search_providers.py    # 可插拔搜索后端
mcp_server.py          # MCP 服务器（stdio JSON-RPC）
feedback_store.py      # 线程安全反馈存储
Dockerfile             # 容器构建
docker-compose.yml     # 一键启动
tests/test_core.py     # 单元测试
```

## 测试

```bash
python -m pytest tests/ -v
python eval/benchmark.py     # 10 题 benchmark（裸 OV vs curator v2）
```

## 和 LangChain / LlamaIndex 有什么区别？

它们构建 RAG **管道**。Curator 治理**知识**：
- 现有知识够不够？
- 新信息可信吗？
- 来源之间有没有矛盾？

可以和任何 RAG 框架配合使用。治理知识，不是管道。

## License

[MIT](LICENSE)
