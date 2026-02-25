# Curator TODO

记录待处理事项。来源标注在每条后面，完成后删掉。

---

## 🔴 待确认（二号 r2 review 中）

- [ ] **cv_warnings 末尾追加 tradeoff 确认**：external_txt 接近 3000 chars 时 warnings 会被截断，judge 完全看不到——二号是否接受这个设计，或者有更好方案？（r2 进行中）

---

## 🟡 已知 tech-debt / 小问题

- [ ] **`_truncate_to` exact-fit edge case**（`decision_report.py`）：条件 `cur + step > max_width - 1` 对恰好填满的字符串会误截断（如 `ABCD` max_width=4 → `ABC…`）。`_row()` 的严格 `>` guard 让它不触发，但逻辑上不对。改为追踪 final-char exact-fit 时修。

- [ ] **`_resolve_conflict` 单向信任假设**（二号发现）：信任分低 → 偏本地，但没有交叉检查本地知识自身时效性。如果本地知识也过时，低信任外搜内容反而更准。低优先级，等有实际案例再改。

---

## 🟢 P1 功能（下一阶段）

以下来自 `CURATOR_PLAN.md`，按优先级排：

- [ ] **Async ingest**：pipeline + search + judge 异步化，支持并发多路搜索
- [ ] **L0/L1 摘要自动生成**：入库时自动生成 abstract（L0）和 overview（L1），目前靠 OV 自身
- [ ] **更多 search provider**：DuckDuckGo / Tavily 支持，降低对 Grok 的依赖
- [ ] **Coverage 自动调参**：根据历史 query_log 反馈，动态调整 threshold 而不是靠 env var 手调
- [ ] **Usage-based TTL**：被频繁引用的资源自动续期，长期未用的加速过期
- [ ] **Pending review UI / CLI**：`pending_review.jsonl` 目前只能手工看，加个 CLI 工具方便审核

---

## 📥 外部建议 backlog（合理但当前不做）

来自外部 review，评估后暂缓：

- [ ] pydantic-settings v2 重构配置——成本高，当前 `env()` 够用
- [ ] structlog / loguru——引入外部依赖，当前日志够用
- [ ] Pipeline 单例（避免每次 run 重新初始化）——需线程安全，暂缓
- [ ] 评估框架 / benchmark——需要真实数据集，先积累数据
- [ ] PyPI 发布——包结构待整理，P1 之后考虑
- [ ] cross_validate warnings 是否影响 judge——已修（末尾追加），但 judge prompt 未显式提及格式，可以在下一次 prompt 调整时顺便加

---

## ✅ 已完成（存档）

### v0.1.0（2026-02-25）
- feedback reranking（`rerank_with_feedback`）
- enhanced dedup（URL hash + Jaccard）
- Decision Report（CJK-safe ASCII box）
- pyproject.toml + GitHub Release

### v0.1.0 post-release（外部 review + 二号 review）
- 幽灵依赖修复：4 个根目录模块移入 `curator/`
- `max_checks` 自适应默认值
- CHANGELOG.md / .env.example 补全 / README Troubleshooting
- cv_warnings 传给 judge（末尾追加，不占正文预算）
- pending_review.jsonl 持久化
- `_verify_ingest` warning → debug
- assess_coverage 小知识库规模修正
- `CURATOR_MAX_L2_DEPTH` env var
- `import time as _time` 多余 alias 清理
- scale_factor 注释修正（n≥9→1.0x）
