# OpenViking Curator

[English](README.md) / 中文

Curator 是 [OpenViking](https://github.com/volcengine/OpenViking) 的**主动知识治理层**。

传统 RAG 系统是被动的——放什么进去就检索什么。Curator 在上面加了一层智能：

| 功能 | 作用 |
|------|------|
| **覆盖率门控** | 评估本地检索质量，只在不足时触发外部搜索 |
| **外部兜底** | 本地知识不够时通过 Grok 搜索补充 |
| **质量审核** | AI 审核后才入库，防止垃圾知识污染 |
| **信任评分** | 基于反馈的加权排序 + 时间衰减 |
| **冲突检测** | 发现来源之间的矛盾 |
| **新鲜度追踪** | 检测过时知识并触发重新验证 |

## 和 LangChain / LlamaIndex 有什么区别？

它们构建 RAG **管道**——帮你检索和生成。

Curator 专注于知识**治理**——决定：
- 现有知识够不够？要不要外搜？
- 新信息可信度够不够？要不要入库？
- 来源之间有没有矛盾？
- 知识有没有过时？

Curator 可以和任何 RAG 框架配合使用。它治理的是知识，不是管道。

## 快速开始

```bash
# 1. 克隆并配置
git clone https://github.com/ponsde/OpenViking_Curator.git
cd OpenViking_Curator
cp .env.example .env
# 编辑 .env，填入你自己的 API 端点和密钥

# 2. 安装依赖（Python 3.10+）
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. 运行查询
python3 curator_query.py "OpenViking 是什么？和传统 RAG 有什么区别？"
```

### 作为类 MCP 工具集成

`curator_query.py` 设计为可编程调用——从你的 Agent/助手调用即可：

```bash
python3 curator_query.py "你的问题"
```

**输出（JSON）：**
```json
{"routed": false, "reason": "negative_match"}
```
→ 不需要知识库，正常处理。

```json
{"routed": true, "answer": "...", "meta": {"coverage": 0.95, "external_triggered": false}}
```
→ 知识库已回答，使用 `answer` 字段。

内置门控自动跳过日常对话、简单指令和跟进问题。

## 架构

```
查询 → 门控(规则) → 路由(LLM) → 本地检索(OpenViking)
                                      ↓
                              覆盖率 + 质量检查
                                   ↙        ↘
                           足够?          外部搜索(Grok)
                             ↓                 ↓
                           回答           审核 → 入库?
                                                ↓
                                              回答
                                                ↓
                                           冲突检测
                                                ↓
                                           Case 沉淀
```

## 文件结构

| 文件 | 用途 |
|------|------|
| `curator_v0.py` | 核心管道（路由→检索→审核→回答） |
| `curator_query.py` | 一体化查询入口，内置门控 |
| `feedback_store.py` | 反馈存储，带文件锁（线程安全） |
| `metrics.py` | 每次查询的执行指标（JSONL） |
| `memory_capture.py` | 查询后自动沉淀 Case |
| `eval_batch.py` | 批量评测 |
| `freshness_rescan.py` | 来源级新鲜度验证 |
| `schemas/` | Case 和 Pattern 模板 |
| `.env.example` | 环境变量模板（无密钥） |
| `tests/` | 单元测试（`pytest`） |

## 配置

所有配置通过环境变量（见 `.env.example`）：

| 变量 | 必填 | 说明 |
|------|------|------|
| `CURATOR_OAI_BASE` | ✅ | OpenAI 兼容 API 地址 |
| `CURATOR_OAI_KEY` | ✅ | 上述 API 的密钥 |
| `CURATOR_GROK_BASE` | ✅ | Grok 搜索 API 地址 |
| `CURATOR_GROK_KEY` | ✅ | Grok API 密钥 |
| `CURATOR_ROUTER_MODELS` | | 路由模型 fallback 链（逗号分隔） |
| `CURATOR_ANSWER_MODELS` | | 回答模型 fallback 链 |
| `CURATOR_JUDGE_MODELS` | | 审核模型 fallback 链 |
| `OPENVIKING_CONFIG_FILE` | | OpenViking 配置文件路径 |

**无硬编码密钥。** 所有敏感值来自 `.env`（已 git-ignore）。

## 模型降级

路由、审核、回答三个阶段都支持降级链：

```
模型A → (503/500?) → 模型B → (失败?) → 模型C
```

通过 `CURATOR_ROUTER_MODELS`、`CURATOR_JUDGE_MODELS`、`CURATOR_ANSWER_MODELS` 配置。

## 测试

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

## 路线图

见 [ROADMAP.md](ROADMAP.md)。

**已完成：** v0.1–v0.4（路由、反馈、冲突检测、新鲜度、模型降级、单元测试）

**下一步：** 阈值可配置化、存储抽象层、CI/CD、模式合成

## 声明

这是一个积极迭代中的实验性项目。上游依赖 [OpenViking](https://github.com/volcengine/OpenViking) 尚处早期阶段，使用需自行评估风险。
