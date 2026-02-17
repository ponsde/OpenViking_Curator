# OpenViking Curator

[English](README.md) / 中文

Curator 是 [OpenViking](https://github.com/volcengine/OpenViking) 的**上层智能治理模块**。

OpenViking 本身擅长“存储+检索”，但它是被动系统：你放什么，它就存什么。
Curator 负责做主动治理：

- **搜（Search）**：本地覆盖不足时自动外部补充
- **审（Review）**：资料入库前做 AI 质量审核
- **评（Score）**：给资料打信任度/排序信号
- **判（Conflict）**：检测资料冲突（v0.3 已实现）

## 这个项目要解决什么

我们希望 Agent 知识库具备：

1. **自生长**（随真实问题逐步扩充）
2. **可控质量**（不是把所有内容都塞进去）
3. **可追溯**（有来源、有可信度）
4. **可用速度**（日常场景可接受）

## 当前状态（Pilot）

当前粗版 v0 已实现：

- 路由定范围（含 fallback）
- OpenViking 本地检索
- Grok 外部补充搜索
- AI 审核 + 可选回写入库

近期已实现：

- 基于用户反馈的检索调权（v0.2）
- 冲突检测流程（v0.3）
- 衰减/清理与新鲜度重扫工具（v0.4）

## 快速开始

```bash
cp .env.example .env
# 编辑 .env，填入你自己的 endpoint/key
bash run.sh "grok2api 自动注册常见失败原因"
```

## 仓库结构

- `curator_v0.py`：当前试点脚本
- `metrics.py`：执行指标采集（jsonl 报告）
- `feedback_store.py`：反馈存储（up/down/adopt），已接入本地检索调权（v0.2）
- `memory_capture.py`：案例沉淀（自动生成 case）
- `schemas/`：case/pattern 模板
- `.env.example`：环境变量模板（不含密钥）
- `run.sh`：一键运行（自动处理 venv）
- `eval_batch.py`：批量评测脚本
- `maintenance.py`：反馈衰减 + 过期案例检查
- `freshness_rescan.py`：来源级新鲜度重扫（URL 元数据）

## 进阶文档

- 运行策略（混合模式）详见 [`MIXED_MODE.md`](MIXED_MODE.md)

## 迭代路线

- **v0.1**：稳定路由、覆盖率判定、外搜回补
- **v0.2**：反馈驱动的优先级/排序（已加入反馈存储脚手架）
- **v0.3**：知识冲突检测
- **v0.4**：记忆自净化与新鲜度重扫

## 说明

本项目仍处于快速迭代阶段。
