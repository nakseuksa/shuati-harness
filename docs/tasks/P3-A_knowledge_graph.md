# P3-A 任务契约：三层知识网 + 同域可逆并查集 + 双指标（提前并行）

## 上下文
项目：数理自适应刷题 Harness。你负责宏观知识网引擎。PM 批准提前并行启动（依赖仅 Phase 1 基座 + llm_gateway，均已就位；文件所有权与在跑 worker 无交集）。
必读规格：`docs/designs/adaptive_practice_harness.md` 的 **§3.4 全节**、**CUJ 4**、Target File **#10/#17**、Alternative 2A/2B（为什么同域 + 可逆）。设计文档是唯一权威规格。

## 你拥有的文件
- `backend/services/knowledge_graph.py`
- `tests/test_knowledge_graph_dsu.py`
- `backend/services/llm_mocks.py`（**仅允许追加** `'node_judge'` fixture，不得改动既有四个 purpose）

**禁止**触碰其他文件（尤其 ingest_service.py / elo_engine.py 正被并行开发）。**禁止** git commit/push。

## 环境
- 项目根：`/Users/wanghaichen/0_work/刷题harness`；一律用 `.venv/bin/` 可执行文件
- 门禁：`.venv/bin/python -m pytest tests/test_knowledge_graph_dsu.py -v` + ruff/mypy 零告警 + 全量 pytest 无回归
- 测试 USE_MOCK_LLM=True；题目/步骤行直接 SQL 插入构造（不依赖 ingest_service）；tmp_db 夹具

## API 契约（conn 由调用方管理，同 elo_engine 模式）
```python
class CanonicalNodeResolver:
    def __init__(self, conn: sqlite3.Connection): ...
    def resolve(self, node_id: str) -> str: ...   # 穿透 redirect_to_id（跳数≤1 由合并侧保证）

async def match_or_create_l3_node(
    conn, l1: str, l2: str, candidate_l3: str, candidate_aliases: list[str],
) -> tuple[str, bool]: ...   # (node_id, reused)
# 同父 L2 活跃节点（redirect_to_id IS NULL）：≤25 全量送 AI 裁判；>25 先字符 2/3-gram Jaccard
# （含 aliases）预筛 Top-15（settings.L3_TOP_K_CANDIDATES/L3_LOCAL_RECALL_TRIGGER_COUNT）。
# AI 裁判 = gateway.chat_json(purpose='node_judge', schema=NodeJudgeResult{match_node_id: str|None, evidence: str})。
# 命中 → 复用主 ID + 新名称并入 aliases（上限 settings.MAX_ALIASES_PER_NODE）；未命中 → uuid4 新建。
# 新建累计满 settings.COMPACTION_BATCH_THRESHOLD（自上次该 L2 compaction 以来，用 created_at 与
# compaction_audit_ledger 最近批次时间推导，无批次则全部计数）→ 自动触发 run_scoped_compaction。

def run_scoped_compaction(conn, l1: str, l2: str) -> str | None: ...  # 返回 batch_id，无合并返回 None
# 严禁跨 L1/L2 重定向（函数签名即作用域，内部再断言）。近义对 = Jaccard 预筛 + AI 裁判确认。
# 每对合并单事务：
#  1) 路径压缩：from_node 及所有指向 from 的子节点 redirect_to_id = to_node（跳数≤1）
#  2) 标红边：端点 resolve 后 u'==v' → 删自环；与既有边重合 → 累加 active_error_count、并 source_question_uids；
#     合并前快照写 previous_co_errors_snapshot_json
#  3) 审计：compaction_audit_ledger 一行（batch_id 同批共用 UUID、ai_judge_evidence、
#     previous_aliases_to_json、affected_child_node_ids_json）
# 节点展示状态为派生值（见 node_state），不落库。

def revert_compaction_batch(conn, batch_id: str) -> None: ...
# 按 merge_id 逆序恢复：from_node.redirect=NULL、affected 子节点指回 from、to_node.aliases 还原、
# 标红边按快照精确还原（删新增/恢复计数与 uids）、is_reverted=1 + reverted_at。
# 已 reverted 的批次再调用 → 幂等无操作。Elo 账本完全不动。

def node_state(conn, node_id: str) -> str: ...  # 'ACTIVE'|'PENDING_RETEST'|'MASTERED'
# 仅聚合挂载的 USER_CANONICAL 且 is_blocked_step=1 步骤所属题目的 mastery_state；
# 悲观半格 ACTIVE > PENDING_RETEST > MASTERED；无此类步骤 → MASTERED（含重定向子节点步骤）

def record_co_error_edges(conn, question_uid: str) -> None: ...
# 该题 ≥2 个不同 resolve 后节点 → 两两边 (u<v)：active+1、historical+1、uids 并入、last_error_at 刷新
def decrement_co_error_edges_on_mastery(conn, question_uid: str) -> None: ...
# 对应边 active-1（下限 0），historical 不动

class GraphMetricsDTO(BaseModel):
    mastery_rate: float; convergence: float; active_nodes: int
def compute_graph_metrics(conn, l1_module: str) -> GraphMetricsDTO: ...
# MasteryRate = (mastered + 0.5*pending)/total*100，仅统计挂载 USER_CANONICAL 题目的活跃主节点
# Convergence：最近 W=20 道 USER_CANONICAL 题（w<5 → ρ=0）；k_reuse=其中含 reused_existing_node=1
# 步骤的题数；ρ=k_reuse/w；p_i=节点挂载 USER_CANONICAL 步骤数/模块总步骤数（0ln0=0）；
# N<3 → η=0，否则 η=H/ln(N)；Convergence=ρ*(0.4+0.6η)*100
```

## 测试要求（设计 #17 逐条 + 补充）
- 跨 L1 / 跨 L2 合并请求硬拒（构造恶意 redirect 尝试）
- 路径压缩后任意节点跳数 ≤1；二级子节点两跳指针正确
- node_state：仅 blocked 步骤聚合；前缀已掌握步骤节点保持 MASTERED；悲观序 ACTIVE>PENDING>MASTERED
- 标红边：自环消除、重合累加、u<v 约束
- **revert 精确性**：合并→回滚后 redirects/aliases/co_errors 与合并前快照逐字段相等；重复回滚幂等；Elo 账本零变化
- 召回：>25 节点时 Jaccard Top-15 截断（构造 30 节点验证裁判只收到 15）；命中复用 aliases 上限 8
- 阈值触发：新建满 15 自动 compaction（Mock 裁判返回合并对）
- 指标冷启动：N=1 / w=2 → 0.0 无除零 NaN；熵均匀分布 → η=1；单点坍缩 → η→0
- co_error 生命周期：record 递增 / mastery 递减 / historical 不减

## 完成标准
门禁全绿 + 全量无回归。最终消息汇报：文件、测试数、门禁摘要、不变量覆盖清单。
