# P4-F 任务契约：生产集成缺口修复（标红边接线 + 盲测 LaTeX 等价）

## 上下文
项目：刷题 Harness。E2E 验收（526 测试、96.77% 覆盖）发现两个生产集成缺口，PM 批准交付前修复。设计文档是唯一权威规格（CUJ 3/4）。

## 缺口与修复方案
### 缺口 1：标红边无生产调用方（违反 CUJ 4）
`knowledge_graph.record_co_error_edges` 无任何生产调用——综合错题录入时跨考点标红边不生长。
**修复**：`ingest_service.ingest_question` 在步骤落库后、事务内，对 `initial_state == "错题"` 且 `error_category != 'ALREADY_MASTERED'` 的新题调用 `record_co_error_edges(conn, question_uid)`（服务内部已处理 ≥2 不同 resolve 节点才建边）。重入库短路路径不重复调用。

### 缺口 2：盲测双重核验不支持 LaTeX（违反 CUJ 3 原题回退分支）
`blind_drill_scheduler` 直接对原始 LaTeX 调 `verify_equation_equivalence`（字符黑名单封禁反斜杠/下划线）→ 真实 LaTeX 标准答案下原题回退分支永远无法通过。scaffold_engine 已有完整匹配栈但未共享。
**修复**：把 scaffold_engine.py 的 `_latex_to_plain` / `_normalize_answer` / `_answers_equivalent` 及其专属正则常量**迁移**到 `backend/sandbox/sympy_verifier.py`，公开命名 `latex_to_plain` / `normalize_answer` / `answers_equivalent`（纯文本处理+等价判定，无服务依赖，属沙盒模块职责）。scaffold_engine 改为从 sympy_verifier 导入（删除私有副本，调用点语义不变）；blind_drill_scheduler 的最终答案与中间量核验（约 L479/L488 两处）改用 `answers_equivalent`。

## 你拥有的文件
- `backend/sandbox/sympy_verifier.py`（追加迁移函数）
- `backend/services/scaffold_engine.py`（删私有副本改导入）
- `backend/services/blind_drill_scheduler.py`（两处核验改 answers_equivalent）
- `backend/services/ingest_service.py`（接线 record_co_error_edges）
- `tests/test_answer_matching.py`（新建：迁移栈单测）
- `tests/test_blind_drill_scheduler.py`（追加 LaTeX 双核验用例）
- `tests/test_ingest_service.py`（追加标红边生长用例）

**禁止**触碰其他文件。**禁止** git commit/push。

## 环境
- 项目根：`/Users/wanghaichen/0_work/刷题harness`；一律用 `.venv/bin/` 可执行文件
- 门禁：全量 `.venv/bin/python -m pytest -q --cov=backend --cov-fail-under=85` + ruff + mypy 全绿（基线 526，只增不减）

## 测试要求
- 迁移栈：LaTeX 分数/根号/下标/希腊命令归一化、蕴含链取结论、链式等式首尾、裸值对单方程右端、字符串相等快路径（从 scaffold 既有测试语义平移，确保行为零漂移）
- **盲测 LaTeX 回归**（缺口 2 复现→修复）：标准答案 `\frac{mv^2}{2} = qU` 类 LaTeX，用户提交 LaTeX 或归一化纯文本 → 原题回退双核验通过；中间量 LaTeX 同样可判
- **标红边生长**（缺口 1 复现→修复）：综合错题（≥2 个不同节点 blocked 步）录入 → concept_co_errors 出现 u<v 边；单节点题不建边；ALREADY_MASTERED 不建边；重入库不重复递增
- scaffold 既有 28 测试、blind 33 测试、E2E 11 测试全部保持绿（行为零漂移证明）

## 完成标准
全量门禁绿（≥526+新增）。最终消息汇报：改动文件、新增测试数、覆盖率、两缺口修复前后对照。
