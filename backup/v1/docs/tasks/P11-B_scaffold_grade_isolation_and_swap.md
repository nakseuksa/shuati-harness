# P11-B 任务契约：脚手架本地检索学段隔离与「换一题」防循环排重

## 上下文

Review 发现脚手架引擎两处缺陷：
1. **Bug 3（跨学段串题）**：`scaffold_engine._search_local_variant` 在 `ISOMORPHIC` 与 `SHARED_TRAP` 分支查询 `questions` 表时未加 `target_grade_and_subject = ?` 过滤（尤其是全局共用 6 种 `TRAP_*` 标签，会把「高中物理」错题召回给「小学奥数」作为变式并建跨学段边）；`_search_local_scaffold` 的 `question_relations` 与 `questions` 查询也应补齐显式学段过滤作为纵深防御。
2. **Bug 4（「换一题」超过 1 次后乒乓死循环）**：`retry_prewarm(..., force=True)` 仅将会话当前的 `prewarm_scaffold_uid` / `prewarm_variant_uid` 放入 `exclude_uids`；生成新题并覆写会话字段后，被换掉的旧题仍留在 `question_relations` 表中。第 2 次点击「换一题」时旧题不在 `exclude_uids` 内，`_search_local_scaffold` / `_search_local_variant` 会立即召回第 1 题，形成 $A \leftrightarrow B$ 死循环。

## 修改方案

### 1. `backend/db/schema.py`

1. 在 `TABLE_DDLS["diagnostic_sessions"]` 增加列：
   `prewarm_excluded_uids_json TEXT NOT NULL DEFAULT '[]'`
2. 为兼容既有历史 SQLite 库，在 `init_db` 中对 `diagnostic_sessions` 执行幂等加列检查（使用既有 `_has_column` 工具）：若缺失 `prewarm_excluded_uids_json`，执行 `ALTER TABLE diagnostic_sessions ADD COLUMN prewarm_excluded_uids_json TEXT NOT NULL DEFAULT '[]'`。

### 2. `backend/services/scaffold_engine.py`

1. **`_search_local_scaffold` / `_search_local_variant`**：
   - 所有检索 SQL（包括先序查 `question_relations JOIN questions` 以及按难度/考点/陷阱查 `questions`）全部显式追加 `q.target_grade_and_subject = ?`（取 `question["target_grade_and_subject"]`），确保绝不跨学段召回或建立跨学段 `question_relations`。
2. **持久化 `prewarm_excluded_uids_json` 防死循环**：
   - 在 `retry_prewarm` 中读取 `session["prewarm_excluded_uids_json"]`（兼容异常/空值回退 `[]`）得到已有排除集合 `excluded: set[str]`。
   - 若 `force=True`：
     - 当 `stage == "STAGE_2_MICRO_SCAFFOLD"` 时，将 `prewarm_scaffold_uid`（若非空）并入 `excluded`；
     - 当 `stage == "STAGE_4_DECRUTCH_VARIANT"` 时，将 `prewarm_variant_uid`（若非空）并入 `excluded`；
     - 其他阶段将当前 `prewarm_scaffold_uid` 与 `prewarm_variant_uid`（若非空）均并入 `excluded`；
     - 在更新会话字段时同步持久化 `prewarm_excluded_uids_json = json.dumps(sorted(excluded), ensure_ascii=False)`。
   - 若 `force=False`（如 FAILED 后重试）：保留既有 `excluded` 并附带当前非空产物 UID（如有）一并作为 `exclude_uids` 传入 `_ensure_prewarm_task`（不清空已排除历史）。

### 3. `tests/test_schema.py` 与 `tests/test_scaffold_engine.py`

- `tests/test_schema.py`：验证 `diagnostic_sessions` 含 `prewarm_excluded_uids_json` 列（默认值 `'[]'`）且对旧库缺少该列时 `init_db` 可幂等补列。
- `tests/test_scaffold_engine.py`：
  1. 新增跨学段隔离测试：两个学段存在相同 `TRAP_*` 标签（及同考点特征）的题目时，`_search_local_variant`（`SHARED_TRAP` / `ISOMORPHIC`）与 `_search_local_scaffold` 绝不命中异学段题目；
  2. 新增连续多次 `retry_prewarm(..., force=True)` 测试：在二阶与四阶分别连续触发 ≥3 次 `force=True` 换一题，每次产出的 UID 均与此前所有历史 UID 互异（不会在 $A \leftrightarrow B$ 间死循环）。

## 文件白名单

- `backend/db/schema.py`
- `backend/services/scaffold_engine.py`
- `tests/test_schema.py`
- `tests/test_scaffold_engine.py`

**禁止**修改白名单之外任何文件；**禁止** git commit/push；**禁止**网络调用。
