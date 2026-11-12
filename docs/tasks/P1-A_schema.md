# P1-A 任务契约：SQLite Schema 基座

## 上下文
项目：数理自适应刷题 Harness（Local-First）。你负责 Phase 1 的数据库基座。
必读规格：`docs/designs/adaptive_practice_harness.md` 的 **§2 核心数据模型与 SQLite 表结构规范（Schema DDL）**（约 150~330 行）与 Target File **#4** 条目。设计文档是唯一权威规格。

## 你拥有的文件（只允许创建/修改这些）
- `backend/db/schema.py`
- `tests/test_schema.py`

**禁止**触碰其他任何文件（config.py、conftest.py、pyproject.toml 已由 PM 就位）。**禁止** git commit/push。

## 环境
- 项目根目录：`/Users/wanghaichen/0_work/刷题harness`
- Python 一律用 `.venv/bin/python`（venv 由 PM 建好；若不存在请报告而不要自建）
- 测试命令：`.venv/bin/python -m pytest tests/test_schema.py -v`
- 质量门禁：`.venv/bin/ruff check backend/db/schema.py tests/test_schema.py` 与 `.venv/bin/mypy backend/db/schema.py` 零告警

## API 契约（下游模块将按此签名调用，不得更改）
```python
def init_db(db_path: Path | None = None) -> None: ...
# db_path 缺省取 backend.config.settings.SQLITE_PATH；幂等（IF NOT EXISTS）；自动创建父目录

@contextmanager
def get_db_connection(db_path: Path | None = None) -> Iterator[sqlite3.Connection]: ...
# 每次连接强制 PRAGMA journal_mode=WAL; foreign_keys=ON; busy_timeout=5000;
# row_factory = sqlite3.Row；正常退出 commit，异常 rollback，最终 close
```

## 实现要求
1. **9 张表逐字段照抄设计 §2 DDL**：`questions`、`question_steps`、`question_relations`、`knowledge_nodes`、`concept_co_errors`、`compaction_audit_ledger`、`diagnostic_sessions`、`question_escrow_pool`、`elo_mutation_ledger`。所有 CHECK 约束、DEFAULT、UNIQUE、外键（含 ON DELETE CASCADE）一字不差。
2. 补充结构化索引（IF NOT EXISTS）：至少覆盖 `questions(mastery_state)`、`questions(primary_l1_module)`、`questions(blind_test_unlock_at)`、`questions(slated_drill_date)`、`question_steps(question_uid)`、`knowledge_nodes(l1_module, l2_entity)`、`diagnostic_sessions(question_uid)`、`elo_mutation_ledger(l1_module)`、`question_relations(target_question_uid, relation_type)`。
3. 常量（表名清单等）如需集中定义放在本文件内；路径/开关类配置一律从 `backend.config.settings` 读取。
4. 完整类型标注 + 简洁中文 docstring。

## 测试要求（tests/test_schema.py，使用 conftest 的 tmp_db 夹具）
- 9 张表全部存在（查 sqlite_master）
- WAL / foreign_keys / busy_timeout 生效
- 外键 enforcement：向 question_steps 插入不存在的 question_uid → IntegrityError
- CHECK 拦截：c_score=5 拒绝；concept_co_errors 自环（u==v）拒绝；u>v 顺序拒绝
- 幂等：init_db 连续调用两次不报错、数据不丢
- 关键默认值：questions.mastery_state='ACTIVE'、verification_status='HUMAN_OR_CANONICAL'、escrow decay_factor=1.0、diagnostic_sessions.micro_stage='STAGE_0_CONFIRM_GATE'
- get_db_connection 异常路径 rollback 验证

## 完成标准
上述测试全绿 + ruff/mypy 零告警。最终消息汇报：创建的文件、测试通过数、门禁输出摘要。
