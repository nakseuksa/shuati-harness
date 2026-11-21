# M1 25 题真机几何压测与 SymPy 沙盒热修裁决记录

- **批次标识**：`20261121_154820_M1_stress_test_25`
- **时间**：2026-11-21 15:48 (Asia/Shanghai)

---

## DEC-031：Stage 1 Prompt JSON 模板升级（`s1_v1` → `s1_v2`）

- **背景**：首轮真机试跑中多模型因未见精确顶层 JSON 键示例导致大量 `SCHEMA_RETRY_FAILED`。
- **裁决**：在 `src/shuati/llm/prompt_builder.py` 补齐精确顶层 JSON 键与 `AnswerPart` 模板，升级 `STAGE1_PROMPT_VERSION = "s1_v2"`；在 `src/shuati/llm/gateway.py` `_build_repair_messages` 注入 `schema.model_json_schema()`。
- **状态**：`EXECUTED`（后续 25 题 Schema 100% 解析通过）。

## DEC-032：SymPy 沙盒双误杀最小化修复与红军 Repro Triage

- **背景**：真机压测暴露 2 类 SymPy 沙盒误杀：
  1. SymPy 内部加减/解方程延迟导入 `sympy.external.gmpy` 被 `_SYMPY_BLOCKED_SUBMODULES` 误拦截；
  2. `ch28_007` 真实脚本返回防御性三元表达式 `str(valid_area[0]) if valid_area else '无解'` 被 `_is_computed_expr` 误判 `VACUOUS_SCRIPT_REJECTED`（同时确认 `ch28_002`/`ch28_020`/`ch28_083` 确为真实空壳，不放行）。
- **裁决**：
  1. 在 `_VERIFY_PREAMBLE` 仅对 SymPy 内部调用方（`caller_name.startswith("sympy.")` 且 `caller_name != "sympy_sandbox"`）白名单放行 `sympy.external.gmpy` / `sympy.external.pythonmpq`，同时保持用户态导入与 `sympy.external.importtools` / `import_module` 全局硬封锁；
  2. 红军审查员（`claude-sonnet-5-5-high`）验证沙盒逃逸红线 `PASS`，并出具 1 项有效反空壳 Repro（`tests/repro/repro_sympy_fix_ifexp_body_sentinel_hardcode.py`，`'A' if y > 3 else str(y)` 绕过）；Orchestrator 裁决有效并指令 `worker-sympy-fix` 将非数值兜底哨兵收紧为严格白名单 `_NON_NUMERIC_FALLBACK_SENTINELS` 且仅限 `orelse` 分支，Repro 退出码转 `0` 并合入 `tests/regression/test_m1_verify_regressions.py`。
- **状态**：`EXECUTED & VERIFIED`。

## DEC-033：模型槽位切换与同库零污染重跑策略

- **背景**：用户将 `.env` 更换为 `vlm_primary=anthropic/claude-sonnet-5.5`、`vlm_secondary=openai/gpt-6.1-terra`、`text_llm/judge_llm=openai/gpt-6.2-sol`。
- **裁决**：直接原库启动新进程重载 `.env` 全量重跑 25 题。依托 `compute_stage1_input_hash()` 自动失效全部 25 题旧缓存，`questions` 与 `question_solutions` 整行/删建幂等覆写，审计流水表按当前 `input_hash` 与本轮时间戳严格隔离统计。
- **状态**：`EXECUTED`。
