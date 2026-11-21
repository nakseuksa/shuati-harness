# M0 独立复核审计执行与实证日志（Audit Execution Log）

- **任务批次**：`M0_independent_audit`
- **落盘时间**：2026-11-21 10:30:00 (Asia/Shanghai)
- **关联文档**：[独立审计报告](20261121_103000_M0_independent_audit_report.md) | [独立审计裁决记录](20261121_103000_M0_independent_audit_decisions.md)

---

## 一、 审计范围与零污染执行原则

- **全量审阅文件**：完整通读 [DESIGN.md](../DESIGN.md)（1511 行）、[REQUIREMENTS.md](../REQUIREMENTS.md)、`pyproject.toml`、`config/*` 全部 4 份配置、`src/shuati/` 全部源文件与 `tests/` 全部 11 个测试模块。
- **只读与隔离纪律**：未修改任何业务源码、配置文件、测试文件或 `data/shuati.db`（实库采用 `file:...?mode=ro&immutable=1` 只读打开，复现实验均在沙盒临时目录内创建后自动销毁）。

---

## 二、 实测命令与真实输出切片

### 1. 基线测试与静态检查复核
- `.venv\Scripts\ruff.exe check --no-cache src tests` → Exit `0` (`All checks passed!`)
- `.venv\Scripts\mypy.exe src` → Exit `0` (`Success: no issues found in 19 source files`)
- `.venv\Scripts\python.exe -X utf8 -m pytest -p no:asyncio -q` → Exit `0` (`53 passed, 2 warnings in 14.72s`)
- 实库只读核验（`inspect_ro.py`）：
  ```text
  tables: 27
  counts: {'raw_questions': 122, 'questions': 122, 'questions_fts': 122, 'compile_state': 610}
  source_file sample: [('题库/ch01_计算基础/problems.jsonl', '1'), ('题库/ch01_计算基础/problems.jsonl', '1')]
  stress_25 total=25, unique=25, missing_in_db=[]
  ```

### 2. 独立缺陷复现实验输出（`audit_repros.py`，Exit Code = `0` 全部断言命中缺陷）

```text
=== Check 1: CLI `shuati reindex` uncommitted transaction rollback ===
CLI stdout: {"ok":true,"command":"reindex","data":{"effective_recomputed":1,"fts_rebuilt":1},"warnings":[],"meta":{"duration_ms":8,"schema_version":1}}
After CLI reindex -> eff_question_text='', eff_format='解答题', fts_count=0
[CONFIRMED BUG #1] `shuati reindex` never commits: 100% of recomputed eff_* and rebuilt FTS rows are rolled back on exit!

=== Check 2: SingleWriterExecutor.async_transaction broken ===
2A: hasattr(ex.async_transaction(conn), '__aenter__') = False (iscoroutinefunction=True)
2B: Worker thread conn.execute raised ProgrammingError: SQLite objects created in a thread can only be used in that same thread.
[CONFIRMED BUG #2] `SingleWriterExecutor.async_transaction` is not an asynccontextmanager (releases lock immediately, never commits/rollbacks) AND crashes with sqlite3.ProgrammingError across threads!

=== Check 3: _match_collection_rule PurePosixPath.match('ch*') hijacking ===
初中物理/ch01_声现象 -> '小学奥数' (expected '初中物理')
初中数学/ch01_有理数 -> '小学奥数' (expected None / unmapped)
[CONFIRMED BUG #3] `PurePosixPath.match('ch*')` matches right-side basename, misclassifying '初中物理/ch01_*' and unmapped '初中数学/ch01_*' into '小学奥数'!

=== Check 4: detect_and_extract_choice_options 2x2 grid drops C & D ===
is_choice=True, opts={'A': '2', 'B': '4'}
[CONFIRMED BUG #4] Standard 2x2 option layout ('A. ...  B. ...\nC. ...  D. ...') silently drops options C and D!

=== Check 5: human_mirror.restore_snapshot leaves extra rows & crashes on practice_log UNIQUE ===
After restore_snapshot -> verify_snapshot is_consistent: False
practice_log restore IntegrityError: UNIQUE constraint failed: practice_log.learner_id, practice_log.client_request_id
[CONFIRMED BUG #5] `restore_snapshot` leaves non-snapshot rows behind (causing `human verify` to immediately fail) AND crashes with IntegrityError on `practice_log(learner_id, client_request_id)` / `uq_practice_voids`!

=== Check 6: Stage 0 duplicate 2-hop chain, image-change orphan miss, & permanent retire ===
6A: resolve_question_uid('q2') -> primary_olympiad:ch01:q1, target in questions=0, q2 in questions=0
6B: After ID rename q1->q2 + apply_retire, active non-retired questions = 0 (expected 1!)
6C: After restoring q1 to problems.jsonl, q1 is_retired=1, reason='MISSING_FROM_SOURCE'
6D: Master changed figure a.png->b.png, q2 duplicate_of_uid='primary_olympiad:ch01:q1', q2 in questions=0 (expected None, 1!)
[CONFIRMED BUG #6A/#6B/#6C/#6D] All 4 Stage 0 deduplication/retirement state-machine bugs reproduced!

=== Check 7: eff_* guard missing on get_connection & format-override options gap ===
7A: Direct UPDATE eff_is_servable on get_connection() succeeded without error -> val=1
7B: After overriding format='选择题', eff_options_json='{}' (expected extracted A/B options!)
[CONFIRMED BUG #7A/#7B] `get_connection` lacks eff guard; overriding `format='选择题'` fails to extract `eff_options_json`!

=== Check 8: Block LaTeX $$...$$, AnswerPart lax validation, search_fts fullwidth/backslash ===
8A normalized $$...$$: '计算：\n$$\nx + A\n$$\n的值'
8B lax AnswerPart accepted: label='(1)-1', sympy_expr=None; collection_mode='ORDERED_TUPLE', items=None
8C search_fts('ＡＢＣ') hits: 0
[CONFIRMED BUG #8A/#8B/#8C] Multi-line `$$...$$` mutated; `AnswerPart` lax; `search_fts` misses fullwidth normalization & backslash escaping!
```
