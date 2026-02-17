# OpenViking Curator

Curator 是 [OpenViking](https://github.com/volcengine/OpenViking) 的**上层智能治理模块**。

OpenViking 本身擅长“存储+检索”，但它是被动系统：你放什么，它就存什么。
Curator 负责做主动治理：

- **搜（Search）**：本地覆盖不足时自动外部补充
- **审（Review）**：资料入库前做 AI 质量审核
- **评（Score）**：给资料打信任度/排序信号
- **判（Conflict）**：检测资料冲突（规划中）

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

尚未实现：

- 基于用户反馈的动态调权
- 完整冲突检测流程
- 长期冷数据衰减/清理策略

## 快速开始

```bash
cp .env.example .env
# 编辑 .env，填入你自己的 endpoint/key
bash run.sh "grok2api 自动注册常见失败原因"
```

## 仓库结构

- `curator_v0.py`：当前试点脚本
- `.env.example`：环境变量模板（不含密钥）
- `run.sh`：一键运行（自动处理 venv）
- `eval_batch.py`：批量评测脚本

## 迭代路线

- **v0.1**：稳定路由、覆盖率判定、外搜回补
- **v0.2**：反馈驱动的优先级/排序
- **v0.3**：知识冲突检测
- **v0.4**：记忆自净化与新鲜度重扫

## 说明

本项目仍处于快速迭代阶段。
