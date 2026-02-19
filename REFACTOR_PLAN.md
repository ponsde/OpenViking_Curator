# Curator × OpenViking 深度整合重构计划

## OV 能力清单（实测确认）

### 检索
| API | 功能 | 返回 | 延迟 |
|-----|------|------|------|
| `find(query, target_uri, limit)` | 纯语义检索，不需 session | memories + resources + skills，每个带 uri/score/abstract/is_leaf/relations | ~1.5s |
| `search(query, session_id, limit)` | VLM 意图分析 + 层次化检索 | 同上 + query_plan(reasoning + TypedQueries) + query_results | ~10s |
| `grep(uri, pattern)` | 文本模式匹配 | 匹配行 | 快 |
| `glob(pattern, uri)` | 文件名匹配 | URI 列表 | 快 |

### 内容分层
| 层 | API | token 量 | 用途 |
|----|-----|----------|------|
| L0 | `abstract(uri)` | ~100 | 快速过滤，已在 find/search 结果的 abstract 字段里 |
| L1 | `overview(uri)` | ~2k | 判断相关性、理解结构 |
| L2 | `read(uri)` | 不限 | 完整内容，按需加载 |

### Session 管理
| API | 功能 |
|-----|------|
| `POST /sessions` | 创建 session |
| `POST /sessions/{id}/messages` | 加消息（role + content 字符串） |
| `POST /sessions/{id}/commit` | 归档 + 提取记忆（6 类：profile/preferences/entities/events/cases/patterns） |
| `POST /sessions/{id}/extract` | 只提取记忆不归档 |

### 存储
| API | 功能 |
|-----|------|
| `POST /resources` | 添加资源（文件/URL/目录），自动解析 + 生成 L0/L1 + 向量化 |
| `POST /skills` | 添加技能 |
| `POST /relations/link` | 建立资源间关联 |
| `ls / stat / tree / mkdir / mv / rm` | 文件系统操作 |

### 核心发现（实测）
1. **find() 结果自带 abstract**（L0），不需要单独调 abstract API
2. **search() 返回 query_plan**：VLM 把一个问题拆成多个 TypedQuery，分 resource/memory/skill 三路
3. **search() 返回 10 结果 vs find() 3 结果**，质量更高（VLM 理解意图）
4. **commit() 自动提取记忆**到 `viking://user/memories/` 和 `viking://agent/memories/`
5. **HTTP API 的 add_message 用 content 字符串**，不是 Part 对象
6. **relations 当前为空**，需要我们主动 link
7. **memories 目录已有 entities 子目录**（commit 生成的）
8. **skills 目录为空**，我们还没用过

---

## 当前问题

### retrieval.py（300+ 行）做了 OV 已经能做的事
- 缩写展开 → OV 的 VLM 意图分析天然理解缩写
- 锚点映射 → OV 的层次化检索按目录递归，不需要手工映射
- 关键词覆盖率计算 → OV 返回 score，直接用
- 本地关键词索引 → 维度修好后 OV 检索确定了，不需要兜底
- 噪声过滤硬编码 → OV 的 score 排序自然过滤低分结果

### pipeline.py 的 session 集成位置不对
- `_intent_search()` 里每次搜索就 commit → 太频繁，消息太少没东西提取
- assistant 回写放在 finally 里 → 应该是核心流程的一部分
- SyncOpenViking 嵌入模式 → session search 死锁 → 要用 HTTP API

---

## 重构方案

### 整体架构
```
用户问题
  ↓
Step 1: Router（Grok LLM 判断是否需要查知识库）
  ↓ 需要
Step 2: OV Session Search（HTTP API，主力）
  - POST /search/search + session_id
  - VLM 意图分析 → TypedQuery → 层次化检索
  - 返回 memories + resources + skills（三路）
  ↓
Step 3: Context 加载
  - find/search 结果已带 abstract（L0）
  - top 结果用 overview（L1）判断相关性
  - 确认需要的才 read（L2）
  ↓
Step 4: 覆盖率评估
  - 基于 OV 返回的 score 和结果数量
  - score 平均 > 0.5 且 >= 2 个结果 → 够用
  - 不够 → Step 5 外搜
  ↓
Step 5: 外搜（可选）
  - Grok 搜索 → 交叉验证 → 审核入库
  - 入库用 POST /resources
  ↓
Step 6: 生成回答
  ↓
Step 7: Session 反馈
  - add_message("assistant", 回答摘要)
  - 累积 >= 5 条消息 或 距上次 commit > 1h → commit
```

### retrieval.py 重写（~100 行）

```python
"""检索：OV session search 为主力，find() 为快速降级。"""

def ov_search(query: str, session_id: str = None, limit: int = 10) -> dict:
    """主力检索：通过 HTTP API 调 OV search。
    有 session_id → 走 VLM 意图分析（10s，更精准）
    无 session_id → 走 find（1.5s，纯语义）
    """
    # POST http://127.0.0.1:9100/api/v1/search/search 或 find
    # 返回 {"memories": [...], "resources": [...], "skills": [...]}

def load_context(results: list, query: str, max_l2: int = 3) -> str:
    """分层加载内容。
    1. 结果自带 abstract（L0）→ 已有
    2. top 结果取 overview（L1）→ GET /content/overview
    3. 最相关的 max_l2 个 → read（L2）→ GET /content/read
    """

def assess_coverage(results: dict) -> tuple[float, bool]:
    """基于 OV score 评估覆盖率。
    返回 (coverage_score, need_external)
    """
```

### pipeline.py 改动
1. 初始化改用 HTTP client（requests 或 urllib），不用 SyncOpenViking
2. 检索直接调 `ov_search()`
3. 覆盖率用 `assess_coverage()`
4. session 管理独立模块（创建/消息/commit 策略）
5. 删掉 dedup 步骤（OV commit 自带去重判断：CREATE/UPDATE/MERGE/SKIP）

### 新增 session_manager.py（~60 行）

```python
"""Session 生命周期管理。"""

class SessionManager:
    def __init__(self, base_url, session_id_file):
        self.base_url = base_url
        self.session_id = self._load_or_create()
        self.msg_count = 0
        self.last_commit_time = time.time()

    def add_user_query(self, query: str)
    def add_assistant_response(self, answer: str, used_uris: list)
    def maybe_commit(self)  # >= 5 msgs 或 > 1h
    def search(self, query: str, limit: int = 10) -> dict
```

### 删除
- `_local_index_search()`、`.curated_index.json`
- `_ABBR_MAP` 缩写展开
- `_anchors` 锚点映射
- `deterministic_relevance()` 手工打分
- 关键词覆盖率计算（`kw_cov`, `core_cov`）
- 噪声过滤硬编码（`NOISE_PATTERNS`）
- `_MockResult` 类
- pipeline 里的 dedup 步骤

### 保留
- `router.py`（LLM 路由不变）
- `search.py`（外搜逻辑不变，但触发条件简化）
- `answer.py`（回答生成不变）
- `feedback.py`（uri_trust_score 对排序有参考价值，可选保留）

---

## 测试计划
1. 重建完成 ✅ → 确定性 OK ✅
2. 写 session_manager.py
3. 重写 retrieval.py
4. 改 pipeline.py
5. 跑 benchmark 对比（新 vs 旧）
6. 46 个单元测试
7. 3-5 个端到端真实查询

## 风险
- search() 需要 10s，如果 VLM 挂了需要 fallback 到 find()（1.5s）
- HTTP serve 需要常驻进程（systemd 管理）
- commit 太频繁会产生大量低质量 memory

## 对外依赖
- OV serve 常驻在 127.0.0.1:9100
- 需要 systemd service 保活
