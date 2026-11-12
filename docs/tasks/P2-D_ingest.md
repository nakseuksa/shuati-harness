# P2-D 任务契约：录入落库服务（Ingest Persistence）

## 上下文
项目：数理自适应刷题 Harness。你负责把「题干+草稿 → 诊断结果」持久化为题目/步骤/会话三表记录，并产出步骤对齐确认卡数据。这是 PM 批准的新增文件（设计将落库隐含在 API_Ingest 中，为保持 Phase 4 路由轻薄而独立成服务）。
必读规格：`docs/designs/adaptive_practice_harness.md` 的 **CUJ 1**、**§3.1 第 1/4 条**、Target File **#5/#8**。设计文档是唯一权威规格。
前置已就位：schema（9 表）、cas_store、difficulty_rubric、vlm_diagnosis（P2-B 产出，含 DiagnosisPayload/StepSkeletonItem Schema 与 analyze_question_and_draft）。

## 你拥有的文件
- `backend/services/ingest_service.py`
- `tests/test_ingest_service.py`

**禁止**触碰其他文件。**禁止** git commit/push。

## 环境
- 项目根：`/Users/wanghaichen/0_work/刷题harness`；一律用 `.venv/bin/` 可执行文件
- 门禁：`.venv/bin/python -m pytest tests/test_ingest_service.py -v` + ruff/mypy 零告警
- 测试用 monkeypatch 替换 `analyze_question_and_draft`（async）返回固定 DiagnosisPayload，不发真实 LLM；图片用 Pillow 合成 + cas_dir 夹具；数据库用 tmp_db 夹具

## API 契约
```python
class GateStepDTO(BaseModel):
    step_index: int; step_goal_desc: str
    status: Literal["completed", "blocked", "untouched"]
    sub_elo: int

class GateCardDTO(BaseModel):
    session_id: str; question_uid: str
    steps: list[GateStepDTO]
    error_category: str; trap_taxonomy_tag: str | None
    floor_preview_elo: int | None      # 已完成前缀的保底水位（last_valid>=1 时取 steps[last_valid-1].sub_elo）
    base_elo: int; tier: str
    is_reimport: bool

async def ingest_question(
    stem_md: str,
    stem_images: list[Path],
    draft_images: list[Path],
    target_grade: str,
    initial_state: Literal["错题", "已掌握"] = "错题",
    base_dir: Path | None = None,
    db_path: Path | None = None,
) -> GateCardDTO: ...
```

## 落库流程（单事务）
1. `normalize_markdown_with_frontmatter` → (md, uid, stem_hash)；草稿图片逐张 `ingest_image_bytes` → cas URI 列表。
2. **幂等重入库**：uid 已存在 → 比对 stem_hash：相同则直接返回既有最新会话的确认卡（is_reimport=True，不新建任何行）；不同则仅更新 stem_md/stem_hash/updated_at，**绝不触碰** mastery_state、盲测字段、Elo 相关数据；若本次携带新草稿图片 → 新建一条 diagnostic_sessions（旧会话保留）。
3. 新题：查 knowledge_nodes 活跃节点构造 existing_l1_l2_tree（dict[l1, list[l2]]）→ `await analyze_question_and_draft(...)`。
4. `calculate_base_elo(trace.c_score, ...)` → base_elo/tier；逐步 `clamp_substep_difficulty` → 子步四维与 sub_elo。
5. **知识点节点（临时朴素匹配，Phase 3 由 CanonicalNodeResolver 升级）**：按 (l1,l2,l3) 精确匹配活跃节点（redirect_to_id IS NULL），命中复用并置 reused_existing_node=1，未命中 uuid4 新建。
6. 写 questions（origin_type='USER_CANONICAL'，mastery_state='ACTIVE'，verification_status='HUMAN_OR_CANONICAL'，trap_tags_json=trace.trap_tags）；写 question_steps（canonical 分支 + alt_branch_steps 若有则 user_alt_1 分支；is_blocked_step = step_index >= stalled_step_index）；写 diagnostic_sessions（micro_stage='STAGE_0_CONFIRM_GATE'，prewarm_status='IDLE'，error_category：initial_state='已掌握' 时强制 'ALREADY_MASTERED'，否则取 alignment）。
7. 组装 GateCardDTO：status 按 last_valid/stalled 推导（<=last_valid → completed；==stalled → blocked；其余 untouched）。
8. **不在录入时结算任何 Elo**（保底/掌握结算发生在 confirm-gate，P2-E 负责）。

## 测试要求
- 新题全链路：三表行数/字段正确、gate 卡 status 推导、floor_preview（stalled=2 → 取 step1.sub_elo；last_valid=0 → None）
- 空白草稿：alignment 默认值落库（0/1/CONCEPT_BOTTLENECK）
- 重入库幂等：同内容二次 ingest 不新增行、返回同 session；改题干文字 → stem 更新但 mastery/盲测字段不变；补新草稿 → 新会话旧会话保留
- initial_state='已掌握' → error_category='ALREADY_MASTERED'
- 一题多解：alt 分支步骤落库且 branch_id='user_alt_1'
- 节点复用：同 (l1,l2,l3) 第二题 reused_existing_node=1 且节点数不增
- 事务原子：monkeypatch 使 steps 写入中途抛错 → questions 无残留

## 完成标准
门禁全绿。最终消息汇报：文件、测试数、门禁摘要。
