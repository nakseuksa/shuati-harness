# M0（工程基座与 Stage 0 入库体检）独立复核审计报告

- **任务批次**：`M0_independent_audit`
- **独立审计结论**：**`CONDITIONAL PASS（主干达标，检出 3 项 P0 高危缺陷 + 5 项 P1 中危缺陷 + 5 项 P2 执行偏差，建议进入 M1 前先行修复）`**
- **审计时间**：2026-11-21 10:30:00 (Asia/Shanghai)
- **详细索引**：[审计裁决与分级依据](20261121_103000_M0_independent_audit_decisions.md) | [独立实证复现记录](20261121_103000_M0_independent_audit_execution.md) | [总索引](INDEX.md)

---

## 一、 M0 主干完成度核验（已通过项）

| 核验维度 | 状态 | 独立核验实证结论 |
| :--- | :---: | :--- |
| 1. 20 表 + FTS5 `trigram` DDL 与 Bootstrap | **PASS** | [schema.py](../../src/shuati/db/schema.py) 完整对齐 [DESIGN.md](../DESIGN.md) §3.2，`user_version=1`，`小学奥数` / 6 大 `TRAP` 种子 / `default` 角色顺序正确 |
| 2. 本地 122 道实题首次入库基线 | **PASS** | 实库只读核验：`raw_questions=122`、`questions=122`、`questions_fts=122`、`compile_state=610`，113 张 CAS 图片保序无损；`ch28_003`/`ch28_016` 选择题纠偏、`ch28_031` 作图题映射、`ch01_012`/`ch28_083` 多图题注、`ch28_015`/`ch28_057` 重扫对（`dHash=1`）、`stress_test_25.yaml` 25 题存在性全部吻合 |
| 3. 四维非线性难度合成（`difficulty.py`） | **PASS** | 108 种 `(C,S,T,L)` 组合、决策 D4 `2400/2600` 阈值、`L5-2_竞赛超难` 档名及 `bool`/`float` 入参拦截完全准确 |
| 4. 只读保护与静态检查基线 | **PASS** | `backup/v1/` 与 `题库/` 零污染；既有 53 项 `pytest`、`ruff`、`mypy` 均通过 |

---

## 二、 独立检出的缺陷与偏差分级清单（前序审查漏检项，均已实证复现）

### P0：高危技术错误与功能 Bug（阻塞后续阶段或导致静默数据失真）

1. **`shuati reindex` 漏调 `conn.commit()` 导致全量重算 `eff_*` 与重建 FTS5 在退出时 100% 自动回滚**
   - **定位**：[main.py](../../src/shuati/cli/main.py#L329-L366)、[effective.py](../../src/shuati/core/effective.py#L466-L480)、[repository.py](../../src/shuati/db/repository.py#L99-L124)
   - **问题**：`recompute_all()`、`rebuild_fts()` 与 CLI `reindex` 均未执行 `conn.commit()`；命令打印 `ok=true` 后进程退出触发 SQLite 隐式 `ROLLBACK`，磁盘数据毫无变化。
2. **`SingleWriterExecutor.async_transaction` 存在三重技术错误，M1 异步写库必崩**
   - **定位**：[connection.py](../../src/shuati/db/connection.py#L148-L173)
   - **问题**：① 非 `@asynccontextmanager`，执行 `BEGIN IMMEDIATE` 后立即释放 `_async_lock` 返回 `conn`，且无 `commit/rollback`；② `run_in_executor` 跨线程使用默认 `check_same_thread=True` 的连接，确定性抛出 `sqlite3.ProgrammingError`；③ `_sync_lock` 与 `_async_lock` 未互通互斥。
3. **`_match_collection_rule` 误用 `PurePosixPath.match("ch*")` 劫持其他学段目录并击穿“未映射拒入”护栏**
   - **定位**：[stage0_ingest.py](../../src/shuati/pipeline/stage0_ingest.py#L61-L74)、[collections.yaml](../../config/collections.yaml#L1-L5)
   - **问题**：`PurePosixPath.match("ch*")` 按右侧末级目录名匹配，导致 `"初中物理/ch01_声现象"` 被第 1 条规则提前劫持为 `'小学奥数'`，且未映射集合（如 `"初中数学/ch01_有理数"`）不会拒入而直接混入 `'小学奥数'`，违背 [DESIGN.md](../DESIGN.md) §3.4。

### P1：中危状态机漏洞、数据边界 Bug 与学科配置错误

4. **`human_mirror.restore_snapshot` 未清理快照外多余行 + 漏处理 `practice_log` 二级唯一约束**
   - **定位**：[human_mirror.py](../../src/shuati/db/human_mirror.py#L152-L331)
   - **问题**：① 仅对快照内行做 UPSERT，回滚恢复时库内新增行残留，导致恢复后紧接运行 `shuati human verify` 直接失败（`is_consistent=False`）；② 漏处理 `practice_log` 的 `UNIQUE(learner_id, client_request_id)` 与 `uq_practice_voids`，不同 `log_id` 撞键时抛 `IntegrityError`。
5. **Stage 0 去重与下架状态机存在 4 处联动漏洞**
   - **定位**：[stage0_ingest.py](../../src/shuati/pipeline/stage0_ingest.py#L416-L753)、[stage0_ingest.py](../../src/shuati/pipeline/stage0_ingest.py#L1010-L1042)
   - **问题**：① **二级 `duplicate_of_uid` 断链**（主题变更为另一题副本被删主表行后，原副本指向空壳 UID）；② **题号重编 + `--apply-retire` 吞题**（新 ID 先判为旧 ID 副本不建主表行，旧 ID 随后在同轮被标记 `is_retired=1`，导致题目从活跃池彻底消失）；③ **`MISSING_FROM_SOURCE` 无法解冻**（源题重新放回后，`raw_questions.is_missing_from_source` 归 0，但 `questions.is_retired` 永远卡在 1）；④ **主题换图漏晋升**（第 595 行孤儿判据仅比对不含图哈希的 `plain_fingerprint`，单图题换为完全不同的配图或双图调序后原精确副本仍挂在旧主题下）。
6. **`detect_and_extract_choice_options` 在标准“两行两列（2×2）”排版下静默丢失 `C`、`D` 选项**
   - **定位**：[text_norm.py](../../src/shuati/core/text_norm.py#L134-L201)
   - **问题**：对 `A. 2    B. 4\nC. 6    D. 8` 排版，横向正则在首行匹配到 `A/B` 后于第 199 行立即 `return`，丢弃次行 `C/D` 选项。
7. **`eff_*` 写守卫未绑定 `get_connection` + 单独覆写 `format='选择题'` 漏提选项 + `node_level='L3'` 未过滤**
   - **定位**：[connection.py](../../src/shuati/db/connection.py#L22-L48)、[effective.py](../../src/shuati/core/effective.py#L93-L106)、[effective.py](../../src/shuati/core/effective.py#L232-L240)
   - **问题**：① 直接调 `get_connection()` 打开既有库时无 `set_authorizer`/`TEMP TRIGGER` 保护；② 仅覆写 `format='选择题'`（未改 `question_text`）时不重提 `eff_options_json`；③ `_resolve_canonical_node_id` 未过滤非 `L3` 或不存在节点。
8. **`config/method_blacklist.yaml` 误将“待定系数法”列入泛词黑名单**
   - **定位**：[method_blacklist.yaml](../../config/method_blacklist.yaml#L11)
   - **问题**：“待定系数法”是高区分度的核心代数/数列/函数求解技法，列入黑名单将在 Stage 2 误删有效方法标签。

### P2：执行偏差与契约细节缺口

9. **`CompileLock` 覆盖缺失**：[main.py](../../src/shuati/cli/main.py#L254-L376) 中 `reindex` 与 `human restore` 未按 [DESIGN.md](../DESIGN.md) §4.1 获取 `data/.compile.lock`。
10. **跨行 `$$...$$` 块级公式与 `search_fts` 查询串处理偏差**：[text_norm.py](../../src/shuati/core/text_norm.py#L57) 未保护跨行 `$$...$$`；[repository.py](../../src/shuati/db/repository.py#L126-L175) `search_fts` 未对查询串做全角转半角规范化，且 `<3` 字 `LIKE ... ESCAPE '\'` 漏转义 `\`。
11. **`AnswerPart` 校验偏松（`domain.py`）**：未强校验 `SYMBOLIC+SINGLE` 必填 `sympy_expr`、非 `SINGLE` 必填 `items`、`CHOICE` 必填 `choice_label` 及拦截自造 `(1)-k` 子标签。
12. **配置与路径细节**：CLI `human restore/verify` 未读取 `settings.yaml` 的 `human_mirror` 配置（如 `include_practice_log: false` 隐私开关失效）；`config.py` 解析 `.env` 未剥离行内注释；单集合子目录导入时 `source_collection` 退化、源题更新漏同步 `raw_chapter`。
13. **前序终审已记录的 3 条展示建议属实**：`reindex --effective --json` 补 `diff_count`、`human verify` 明确 `ok`/退出码语义、`ingest_*.md` 列出图片告警明细。

---

## 三、 修复建议与推进路线

建议在正式启动 **M1** 前安排一次约 1 小时的 **M0.1 收口补丁**，一次性修复上述 **P0（#1~#3）与 P1（#4~#8）**（顺带补齐 P2 中的 `CompileLock`、`AnswerPart` 强校验与 `settings.yaml` 联动），并将本次独立审计的 13 个实证用例合入 `tests/regression/`，使 M1 建立在零隐患的底座之上。
