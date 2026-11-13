"""LLM/VLM 网关离线 Mock fixture（PM 锁定路由键，全项目断网开发基座）。

规格：docs/tasks/P2-B_gateway_vlm.md（API 契约）+
docs/designs/adaptive_practice_harness.md §3.1 第 3/4 条、§3.2 内核先行协议。

路由键（purpose）清单：
- ``diagnosis``       完整 DiagnosisPayload：3 步物理题骨架（拱形桥顶 N=0 临界），
                      草稿对齐卡在 Step 2，四维 (3,3,2,3)，trap_tags=['TRAP_DOMAIN_BOUNDARY']。
- ``scaffold_kernel`` 二阶脱壳微母题内核先行 JSON（一元一次方程，默认参数解 x=5）。
- ``variant_kernel``  当场拆拐杖变式内核先行 JSON（矩形周长，默认参数解 x=5）。
- ``solver_blind``    双盲求解答案，与两个内核默认参数下的答案一致（x = 5）。
- ``node_judge``      L3 节点对齐裁判（P3-A）：默认保守未命中（match_node_id=None），
                      测试可 monkeypatch 注入命中 / 合并对场景。

内核协议（设计 §3.2）：``solve_kernel()`` 必须返回
``{"params": dict[str, str | int | float], "answer_latex": str}``；
``sympy_solver_code`` 仅 import sympy/math/fractions，含真实符号求解调用，可过 AST 探针。
``parametric_stem_tpl`` 使用 ``str.format`` 占位符（如 ``{a}``），模板内不含其他花括号，
由下游 scaffold_engine 以 ``tpl.format(**params)`` 回填。

测试可 monkeypatch 本字典注入畸形/失败场景（网关按模块属性在调用期读取）。
"""

from typing import Any

__all__ = ["MOCK_RESPONSES"]

# ---------------------------------------------------------------------------
# diagnosis：拱形桥顶临界速度题（TRAP_DOMAIN_BOUNDARY：遗漏 N≥0 隐含边界，恰脱离时 N=0）
# 题干：汽车以速度 v 通过半径 R=40m 的拱形桥最高点，g=10m/s²，求不脱离桥面的最大速度。
# 草稿：Step 1 正确列出牛顿第二定律；Step 2 误设 N=mg（遗漏 N=0 临界边界）→ 卡在 Step 2。
# ---------------------------------------------------------------------------
_DIAGNOSIS_FIXTURE: dict[str, Any] = {
    "trace": {
        "steps": [
            {
                "step_index": 1,
                "step_goal_desc": "在桥顶对汽车受力分析，沿指向圆心方向列牛顿第二定律方程",
                "step_formula_latex": r"mg - N = \frac{m v^2}{R}",
                "l1_module": "力学",
                "l2_entity": "圆周运动",
                "l3_operator": "向心力来源与牛顿第二定律",
                "c_sub": 2,
                "s_sub": 1,
                "t_sub": 1,
                "l_sub": 1,
                "masked_conditions": [
                    r"拱形桥半径 $R = 40\,\text{m}$",
                    r"重力加速度 $g = 10\,\text{m/s}^2$",
                ],
                "probe_question_md": (
                    "汽车在桥顶做圆周运动，向心力由哪些力的合力提供？"
                    "请沿指向圆心方向列出牛顿第二定律方程。"
                ),
            },
            {
                "step_index": 2,
                "step_goal_desc": "挖掘『不脱离桥面』的隐含临界边界：支持力 N≥0，恰好脱离时 N=0",
                "step_formula_latex": (
                    r"N = 0 \Rightarrow mg = \frac{m v_{max}^2}{R} "
                    r"\Rightarrow v_{max} = \sqrt{gR}"
                ),
                "l1_module": "力学",
                "l2_entity": "圆周运动",
                "l3_operator": "临界边界条件（N=0）",
                "c_sub": 2,
                "s_sub": 2,
                "t_sub": 2,
                "l_sub": 1,
                "masked_conditions": ["汽车不脱离桥面", r"桥面支持力 $N \geq 0$"],
                "probe_question_md": (
                    "汽车恰好要飞离桥面的瞬间，桥面对汽车的支持力 $N$ 等于多少？"
                    "由此写出临界速度 $v_{max}$ 的表达式。"
                ),
            },
            {
                "step_index": 3,
                "step_goal_desc": "代入数值求出最大安全速度并核对单位",
                "step_formula_latex": r"v_{max} = \sqrt{10 \times 40} = 20\ \text{m/s}",
                "l1_module": "力学",
                "l2_entity": "圆周运动",
                "l3_operator": "代数求值与单位核对",
                "c_sub": 1,
                "s_sub": 1,
                "t_sub": 1,
                "l_sub": 2,
                "masked_conditions": [r"$R = 40\,\text{m}$", r"$g = 10\,\text{m/s}^2$"],
                "probe_question_md": (
                    "将 $g$ 与 $R$ 的数值代入临界速度表达式，$v_{max}$ 等于多少？"
                ),
            },
        ],
        "standard_answer_latex": r"v_{max} = \sqrt{gR} = 20\ \text{m/s}",
        "c_score": 3,
        "s_score": 3,
        "t_score": 2,
        "l_score": 3,
        "trap_tags": ["TRAP_DOMAIN_BOUNDARY"],
    },
    "alignment": {
        "draft_steps_latex": [r"mg - N = \frac{m v^2}{R}", "N = mg"],
        "matched_branch_id": "canonical",
        "alt_branch_steps": None,
        "last_valid_step_index": 1,
        "stalled_step_index": 2,
        "error_category": "TRAP_OR_CALC_ERROR",
        "trap_taxonomy_tag": "TRAP_DOMAIN_BOUNDARY",
    },
}

# ---------------------------------------------------------------------------
# 内核先行 fixture：简单代数/几何题，真实可解且可过 AST 探针（设计 §3.2）。
# 两个内核默认参数下均解得 x = 5，与 solver_blind 答案一致。
# ---------------------------------------------------------------------------
_SCAFFOLD_SOLVER_CODE = """import sympy

def solve_kernel():
    a, b, c = 3, 5, 20
    x = sympy.Symbol("x")
    solutions = sympy.solve(sympy.Eq(a * x + b, c), x)
    answer = solutions[0]
    return {"params": {"a": a, "b": b, "c": c}, "answer_latex": f"x = {sympy.latex(answer)}"}
"""

_VARIANT_SOLVER_CODE = """import sympy

def solve_kernel():
    b, p = 3, 16
    x = sympy.Symbol("x")
    solutions = sympy.solve(sympy.Eq(2 * (x + b), p), x)
    answer = solutions[0]
    return {"params": {"b": b, "p": p}, "answer_latex": f"x = {sympy.latex(answer)}"}
"""

MOCK_RESPONSES: dict[str, dict[str, Any]] = {
    "diagnosis": _DIAGNOSIS_FIXTURE,
    "scaffold_kernel": {
        "parametric_stem_tpl": "解关于 $x$ 的一元一次方程：${a}x + {b} = {c}$（$a \\neq 0$）。",
        "sympy_solver_code": _SCAFFOLD_SOLVER_CODE,
        "constraints_check": "a, b, c 为正整数且 a ≠ 0；方程在实数域有唯一解，且解 x 为正整数。",
    },
    # 递归降阶内核（2026-11-13 需求）：一步直达最简方程，与 solver_blind 答案一致（x = 5）。
    "descent_kernel": {
        "parametric_stem_tpl": "解关于 $x$ 的一元一次方程：$2x = 10$。",
        "sympy_solver_code": (
            "import sympy\n\n"
            "def solve_kernel():\n"
            "    x = sympy.symbols('x')\n"
            "    sol = sympy.solve(sympy.Eq(2 * x, 10), x)\n"
            "    return {'params': {}, 'answer_latex': 'x = ' + str(sol[0])}\n"
        ),
        "constraints_check": "一步除法直达，系数整除，无情境干扰；解 x = 5 为正整数。",
    },
    "variant_kernel": {
        "parametric_stem_tpl": "一个矩形的宽为 ${b}$，周长为 ${p}$。设长为 $x$，求 $x$。",
        "sympy_solver_code": _VARIANT_SOLVER_CODE,
        "constraints_check": "b, p 为正整数且 p > 2b（保证长 x > 0）；周长方程有唯一正实数解。",
    },
    # 双盲求解：独立零上下文 Solver 的答案，与两内核默认参数答案一致（x = 5）。
    "solver_blind": {
        "answer_latex": "x = 5",
        "reasoning_brief": "移项得 3x = 20 - 5 = 15，两边同除以 3，得 x = 5。",
    },
    # L3 节点对齐裁判（P3-A，设计 §3.4）：默认保守判未命中 -> 新建节点，
    # 避免 Mock 模式下意外复用/合并；测试按需 monkeypatch 本键注入命中场景。
    "node_judge": {
        "match_node_id": None,
        "evidence": "候选算子与既有节点语义不同一（默认保守判定），应新建节点。",
    },
}
