# AGENTS.md — OpenViking Curator

这个文件是给 AI 协作者（小p、二号、以及未来的 agent）读的。
进仓库之前先读完，了解项目状态和协作规则，不要靠猜。

---

## 项目是什么

**OpenViking Curator** 是 OpenViking（字节跳动开源 Agent 上下文数据库）的知识治理插件层。

核心功能：
- **检索**：从 OV 取本地知识，按 feedback 信号微调排名
- **覆盖评估**：判断本地知识够不够用，还是需要外搜补充
- **外搜 + 审核**：拉取外部内容，cross_validate 风险标注，judge 决定是否入库
- **去重**：URL hash（Layer 1）+ Jaccard 词相似度（Layer 2）
- **决策报告**：每次 pipeline run 输出人类可读的决策摘要

主仓库：`/home/ponsde/OpenViking_Curator`
GitHub：`github.com/ponsde/OpenViking_Curator`
OV 环境：`/home/ponsde/OpenViking_test/.venv`（运行测试用这个 venv）

---

## 关键文件

```
curator/
  config.py          — 所有配置（env var 读取），改阈值/超参从这里入手
  pipeline_v2.py     — 主 pipeline，run() 是入口
  retrieval_v2.py    — OV 检索、feedback reranking、assess_coverage、load_context
  dedup.py           — 两层去重（URL hash + Jaccard）
  decision_report.py — format_report()、format_report_short()，CJK-safe
  search.py          — external_search()、cross_validate()
  review.py          — judge_and_ingest()、detect_conflict()、ingest_markdown_v2()
  feedback_store.py  — per-URI up/down/adopt 信号存储
  metrics.py         — pipeline metrics 收集
  session_manager.py — 长期记忆提取（active_count workaround，等 PR #280 合并后删）
tests/               — 172 个测试，全部在 venv 下跑：python3 -m pytest tests/ -q
```

---

## 当前状态（截至 2026-02-25）

- **版本**：v0.1.0 已发布（GitHub Release）
- **测试**：172 个，全过
- **待确认**：二号 r2 review 进行中（cv_warnings tradeoff）
- **Backlog**：见 `/home/ponsde/.openclaw/workspace/TODO.md`（小p 维护）

### 上游 PR
- PR #280：`fix/active-count-update-signature`，等 volcengine/OpenViking 合并
- 合并后删除 `session_manager.py` 里的 workaround（commit `2d8785a` 的注释有说明）

---

## 关键决策（为什么这么做）

| 决策 | 原因 |
|------|------|
| feedback delta 上限 ±0.10 | OV 原始 score 主导，feedback 只做微调，避免少量信号翻转排名 |
| adopt 信号只记 used_uris | load_context 真正用到的才算采纳，全部检索结果记 adopt 是错的 |
| assess_coverage 用 raw score | feedback-adjusted score 不应影响"是否外搜"的决策 |
| dedup sim=1.0 是 sentinel | url_hash 方法时 sim=1.0 表示"共享来源 URL"，不是"内容100%相同" |
| cv_warnings 追加末尾 | 正文优先占 judge 3000-char 预算；result["external_text"] 保持干净 |
| pending_review.jsonl | judge 通过但被阻止入库的内容不能直接丢，写文件等人工审核 |
| max_checks=0（自适应） | min(50, len(uris)*3)，比固定值 5 合理 |
| 根目录模块移入 curator/ | pip install 场景下绝对 import 会找不到根目录文件，所有模块用相对 import |

---

## 协作规则（小p ↔ 二号）

```
发现问题的人 → 自己修 → 发给对方 review
    ↓
对方审核：代码正确吗？会干扰其他地方吗？
    ↓
  没问题 → LGTM，合并，完成
  有问题 → 改好后发回，附上原因
    ↓
最多 2 轮，解决不了 → 上报主人
```

**原则**：
- 发现 bug 的人自己修，不是推给对方
- 每次返回说清楚**为什么改**
- 改完必须跑 `python3 -m pytest tests/ -q` 确认 172 全过再提交
- commit 信息要说清楚改了什么、为什么

---

## 常见坑

- **OV 入库后立即检索必然 miss**（索引延迟）——`_verify_ingest` 已降级为 debug log，不是 bug
- **`session search` 会死锁**——用 HTTP API 或 `find`，不要用 session search
- **active_count bug**：上游 OV v0.1.18 的 `_update_active_counts()` 签名错误，Curator 有 workaround，等 PR #280
- **`_truncate_to` exact-fit edge case**：`cur + step > max_width - 1` 对恰好填满的字符串会误截断，已记 TODO，目前 `_row()` 的 guard 让它不触发
- **scale_factor 公式**：`min(1.0, 0.75 + 0.03 * n)`，n≥9 才到 1.0（不是 n≥8）

---

## 运行测试

```bash
cd /home/ponsde/OpenViking_Curator
source /home/ponsde/OpenViking_test/.venv/bin/activate
python3 -m pytest tests/ -q
# 期望：172 passed
```
