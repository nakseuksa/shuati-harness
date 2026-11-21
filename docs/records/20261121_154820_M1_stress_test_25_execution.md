# M1 25 题真机几何压测与 SymPy 沙盒热修执行记录

- **批次标识**：`20261121_154820_M1_stress_test_25`
- **时间**：2026-11-21 15:48 (Asia/Shanghai)

---

## 1. 客观时间线

- **14:47** `worker-prompt-fix` 完成 `s1_v2` Prompt 模板与 Gateway Schema 自愈注入修复；`ch28_001` 单题真机冒烟 `Exit=0`。
- **14:54–15:18** 完成第一轮 25 题真机压测，定位 SymPy 沙盒 `sympy.external.gmpy` 与 `ast.IfExp` 误杀两项工程 Bug。
- **15:20–15:30** `worker-sympy-fix`（`gemini-4-hydrogen-high`）完成最小化补丁，全量 `pytest -q` `123 passed`，`ruff`/`mypy` 0 错（交接件：[sympy_fix_handoff.md](../../.agents/teamwork/worker/sympy_fix_handoff.md)）。
- **15:31** 探测用户新换的 5 个 OpenRouter 槽位全部 `200 OK`。
- **15:31–15:34** `reviewer-sympy-fix`（`claude-sonnet-5-5-high`）对抗审查：逃逸红线 `PASS`，提交反空壳 Repro `tests/repro/repro_sympy_fix_ifexp_body_sentinel_hardcode.py`（`Exit=1`）。
- **15:35–15:38** `worker-sympy-fix` 收紧白名单并限定 `orelse` 分支，Repro 转 `Exit=0` 并合入回归测试（31 passed，`ruff`/`mypy` 0 错）；清理全部 Swarm 成员。
- **15:38–15:44** 启动并完成第二轮 25 题真机几何压测（全新 3 模型阵容），`Exit=0`，耗时 `289.855s`。

## 2. 实际变更文件清单

- `src/shuati/llm/prompt_builder.py`（`s1_v2` 模板）
- `src/shuati/llm/gateway.py`（自愈重试注入 JSON Schema）
- `src/shuati/verify/sympy_sandbox.py`（内部 `gmpy`/`pythonmpq` 精准放行 + `IfExp` `orelse` 白名单哨兵）
- `tests/unit/test_sympy_sandbox.py`
- `tests/regression/test_m1_verify_regressions.py`
- `tests/repro/repro_sympy_fix_ifexp_body_sentinel_hardcode.py`
- `.agents/teamwork/worker/sympy_fix_handoff.md`
- `.agents/teamwork/reviewer/analysis.md`
- 运行时数据：`data/shuati.db`、`data/llm_raw/202611/`（`backup/v1/` 与 `题库/` 零修改）

## 3. 关键实测命令与输出切片

```text
$ .venv\Scripts\python.exe tests/repro/repro_sympy_fix_ifexp_body_sentinel_hardcode.py
OK rejected: VACUOUS_SCRIPT_REJECTED  (Exit 0)

$ .venv\Scripts\python.exe -m pytest -q tests/unit/test_sympy_sandbox.py tests/regression/test_m1_verify_regressions.py
31 passed in 30.43s  (Exit 0)

$ .venv\Scripts\ruff.exe check --no-cache src tests
All checks passed!  (Exit 0)

$ .venv\Scripts\mypy.exe --cache-dir "$env:TEMP\shuati_mypy_cache" src
Success: no issues found in 33 source files  (Exit 0)

$ .venv\Scripts\python.exe -m shuati.cli.main compile --stage 1 --stress-test-25 --db data/shuati.db --config-dir config --assets-dir data/assets/images --json
{"ok":true,"command":"compile","data":{"total_candidates":25,"processed":25,"skipped_ok":0,"skipped_human_verified":0,"verified_consensus":17,"needs_review":8,"failed":0,"multi_solution_count":6,"errors":[]},"warnings":[],"meta":{"duration_ms":289855,"schema_version":1}}
(Exit 0)
```

---

## [2026-11-21 20:38] 本地复核页裁决与落盘核对

- **20:26–20:36** 在沙盒外启动 `shuati review --port 8765`，用户完成 25 题压测池复核后停止服务并核验落盘状态：
  - `human_overrides` 活跃记录 **23 条**，已同步导出至 `human/overrides.jsonl`（23 行，20,157 bytes）；
  - **5 题转 `HUMAN_VERIFIED`（`eff_is_servable=1`）**：`ch01_010`、`ch01_012`、`ch01_013`、`ch28_009`（`ADOPT_RUN:PRIMARY`），`ch28_010`（`ADOPT_RUN:SECONDARY`）；
  - **3 题标为坏题剔除（`is_excluded=1`, `eff_is_servable=0`）**：`ch28_025`、`ch28_031`、`ch28_091`（`EXCLUDE_BAD_QUESTION`）；
  - **17 题 `VERIFIED_CONSENSUS`（`eff_is_servable=1`）** 保持共识未发生驳回；其余 **97 题** 维持 `PENDING`。

```text
$ .venv\Scripts\python.exe -c "import sqlite3, json; conn = sqlite3.connect('file:data/shuati.db?immutable=1', uri=True); conn.row_factory = sqlite3.Row; print('STATUS:', json.dumps([dict(r) for r in conn.execute('SELECT m_verification_status, eff_verification_status, eff_is_servable, COUNT(*) as cnt FROM questions GROUP BY 1, 2, 3').fetchall()], ensure_ascii=False)); print('OVERRIDES:', conn.execute('SELECT COUNT(*) FROM human_overrides WHERE is_active=1').fetchone()[0])"
STATUS: [{"m_verification_status": "NEEDS_REVIEW", "eff_verification_status": "HUMAN_VERIFIED", "eff_is_servable": 1, "cnt": 5}, {"m_verification_status": "NEEDS_REVIEW", "eff_verification_status": "NEEDS_REVIEW", "eff_is_servable": 0, "cnt": 3}, {"m_verification_status": "PENDING", "eff_verification_status": "PENDING", "eff_is_servable": 0, "cnt": 97}, {"m_verification_status": "VERIFIED_CONSENSUS", "eff_verification_status": "VERIFIED_CONSENSUS", "eff_is_servable": 1, "cnt": 17}]
OVERRIDES: 23
(Exit 0)
```
