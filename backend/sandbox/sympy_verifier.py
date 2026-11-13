"""受限校验沙盒：AST 静态探针 + SymPy 子进程隔离 + 代数等价引擎。

设计文档 §3.2 第 2 条（内核先行 + AST 静态探针 + 平台兼容沙盒）、§3.3 第 1 条
（代数等价判据）、Target File #7、NFR Security 段、Risks 表 macOS 行：

- ``validate_script_ast``：导入白名单（仅 sympy/math/fractions）、禁用调用、
  双下划线内省拦截、必含真实符号求解/化简调用，否则 VACUOUS_SCRIPT_REJECTED；
- ``run_sympy_in_sandbox``：先过 AST 探针再起独立子进程；平台感知 rlimit
  （Linux RLIMIT_AS + RLIMIT_CPU；macOS 仅 RLIMIT_CPU，RLIMIT_AS 捕获
  (ValueError, OSError) 降级忽略——dyld 共享缓存映射已超数十 GB）；
  超时 os.killpg 强杀整个进程组后返回 TIMEOUT；
- ``verify_equation_equivalence``：parse_expr（隐式乘法 + convert_xor）+
  nsimplify(rational=True)，移项 / 相反数 / 非零常数比率三判据；同样在隔离
  子进程内执行并带超时保护（parse_expr 内部会 eval，绝不在宿主进程执行）；
- ``latex_to_plain`` / ``normalize_answer`` / ``answers_equivalent``：LaTeX 答案
  规范化与等价判定共享栈（纯文本处理 + 等价判定，无服务依赖），供脚手架引擎
  与每日盲测双重核验共用（P4-F：CUJ 3 原题回退分支支持真实 LaTeX 标准答案）。

实现说明（与设计意图的对齐）：
- 子进程入口采用 Popen + communicate(timeout=...)，即 subprocess.run(capture_output=True,
  text=True, timeout=..., start_new_session=True) 的底层原语。subprocess.run 超时只杀
  直接子进程；为满足设计「超时强杀进程组」的硬性要求，必须持有进程句柄以便 os.killpg。
- RLIMIT_CPU 取 ceil(timeout_s) + 1：留 1s 裕量让宿主墙钟超时先触发，
  保证死循环确定性地归类为 TIMEOUT 而非被 SIGXCPU 抢先打成 RUNTIME_ERROR。
- runner 内用户代码的 stdout 重定向到 stderr：既防止伪造 ###RESULT### 哨兵行，
  也把用户 print 留作重试反馈（stderr_tail）。
"""

from __future__ import annotations

import ast
import json
import logging
import math
import os
import re
import signal
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass

from backend.config import settings
from backend.observability import log_event

logger = logging.getLogger(__name__)

_ALLOWED_IMPORT_ROOTS = frozenset({"sympy", "math", "fractions"})
_FORBIDDEN_CALL_NAMES = frozenset(
    {"open", "eval", "exec", "__import__", "getattr", "globals", "locals"}
)
_SOLVER_CALL_NAMES = frozenset(
    {"solve", "solveset", "dsolve", "simplify", "Eq", "integrate", "limit", "Matrix"}
)

_RESULT_SENTINEL = "###RESULT###"
_ERROR_SENTINEL = "###ERROR###"
_STDERR_TAIL_CHARS = 500


@dataclass
class ASTValidationResult:
    """AST 静态探针结论。reason ∈ OK / VACUOUS_SCRIPT_REJECTED / FORBIDDEN_IMPORT /
    FORBIDDEN_CALL / FORBIDDEN_ATTR / SYNTAX_ERROR。"""

    ok: bool
    reason: str


@dataclass
class SandboxExecutionResult:
    """沙盒执行结论。error_kind ∈ TIMEOUT / RUNTIME_ERROR / BAD_CONTRACT / AST_REJECTED / None。"""

    ok: bool
    params: dict[str, str | int | float] | None
    answer_latex: str | None
    error_kind: str | None
    stderr_tail: str


# ---------------------------------------------------------------------------
# 1. AST 静态探针
# ---------------------------------------------------------------------------


def _import_name_allowed(name: str) -> bool:
    """导入根模块必须在白名单内，且路径中不得含双下划线段（防 sympy.__loader__ 走私）。"""
    parts = name.split(".")
    return parts[0] in _ALLOWED_IMPORT_ROOTS and not any(p.startswith("__") for p in parts)


def validate_script_ast(code_str: str) -> ASTValidationResult:
    """对 LLM 生成的 sympy_solver_code 做静态合规探针（不起进程、不执行任何代码）。"""
    if not isinstance(code_str, str):
        return ASTValidationResult(False, "SYNTAX_ERROR")
    try:
        tree = ast.parse(code_str)
    except (SyntaxError, ValueError):
        return ASTValidationResult(False, "SYNTAX_ERROR")

    has_solver_call = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _import_name_allowed(alias.name):
                    return ASTValidationResult(False, "FORBIDDEN_IMPORT")
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0 or node.module is None or not _import_name_allowed(node.module):
                return ASTValidationResult(False, "FORBIDDEN_IMPORT")
            for alias in node.names:
                if alias.name.startswith("__"):
                    return ASTValidationResult(False, "FORBIDDEN_ATTR")
        elif isinstance(node, ast.Name):
            if node.id in _FORBIDDEN_CALL_NAMES:
                return ASTValidationResult(False, "FORBIDDEN_CALL")
            # 加固：双下划线名字（__builtins__ / __loader__ 等）同样属于内省逃逸面
            if node.id.startswith("__"):
                return ASTValidationResult(False, "FORBIDDEN_ATTR")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                return ASTValidationResult(False, "FORBIDDEN_ATTR")
            if node.attr in _FORBIDDEN_CALL_NAMES:
                return ASTValidationResult(False, "FORBIDDEN_CALL")
        elif isinstance(node, ast.Call):
            func = node.func
            name: str | None = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name in _SOLVER_CALL_NAMES:
                has_solver_call = True

    if not has_solver_call:
        return ASTValidationResult(False, "VACUOUS_SCRIPT_REJECTED")
    return ASTValidationResult(True, "OK")


# ---------------------------------------------------------------------------
# 2. 子进程沙盒
# ---------------------------------------------------------------------------


def _build_rlimit_preamble(max_mem_mb: int, timeout_s: float) -> str:
    """生成 runner 头部的平台感知 rlimit 代码（设计 §3.2 第 2 条 / NFR Security）。"""
    cpu_limit = math.ceil(timeout_s) + 1  # 裕量：让宿主墙钟超时先于 SIGXCPU 触发
    mem_bytes = int(max_mem_mb) * 1024 * 1024
    return textwrap.dedent(f"""\
        import resource as _resource
        import sys as _rl_sys
        _CPU_LIMIT = {cpu_limit}
        _MEM_BYTES = {mem_bytes}
        try:
            _resource.setrlimit(_resource.RLIMIT_CPU, (_CPU_LIMIT, _CPU_LIMIT))
        except (ValueError, OSError):
            pass
        if _rl_sys.platform.startswith("linux"):
            try:
                _resource.setrlimit(_resource.RLIMIT_AS, (_MEM_BYTES, _MEM_BYTES))
            except (ValueError, OSError):
                pass
        elif _rl_sys.platform == "darwin":
            # macOS：dyld 共享缓存映射已超数十 GB，RLIMIT_AS 必抛 ValueError；
            # 捕获降级忽略，依靠宿主进程组超时强杀提供保底隔离（Risks macOS 行）。
            try:
                _resource.setrlimit(_resource.RLIMIT_AS, (_MEM_BYTES, _MEM_BYTES))
            except (ValueError, OSError):
                pass
        """)


# runner 受信前导：受限 builtins + 白名单 import 守卫 + 契约校验。
# 用户代码仅在此受限命名空间中 exec；sympy 等白名单模块自身仍用真实 builtins。
_SOLVER_PREAMBLE = '''
import builtins as _builtins
import json as _json
import sys as _sys
import traceback as _traceback

_RESULT_SENTINEL = "###RESULT###"
_ERROR_SENTINEL = "###ERROR###"
_ALLOWED_ROOTS = ("sympy", "math", "fractions")
_real_import = _builtins.__import__


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    parts = name.split(".")
    if level != 0 or parts[0] not in _ALLOWED_ROOTS:
        raise ImportError("sandbox: forbidden import " + repr(name))
    if any(p.startswith("__") for p in parts):
        raise ImportError("sandbox: forbidden import " + repr(name))
    return _real_import(name, globals, locals, fromlist, level)


_DROP_BUILTINS = {
    "open", "eval", "exec", "compile", "getattr", "setattr", "delattr",
    "globals", "locals", "vars", "dir", "input", "breakpoint", "help",
    "exit", "quit", "__import__",
}
_safe_builtins = {k: v for k, v in vars(_builtins).items() if k not in _DROP_BUILTINS}
_safe_builtins["__import__"] = _safe_import


class _ContractError(Exception):
    pass


def _validate_contract(result):
    if not isinstance(result, dict):
        raise _ContractError("solve_kernel() must return a dict")
    params = result.get("params")
    if not isinstance(params, dict):
        raise _ContractError("missing or non-dict 'params'")
    answer = result.get("answer_latex")
    if not isinstance(answer, str) or not answer.strip():
        raise _ContractError("missing or empty 'answer_latex'")
    for key, value in params.items():
        if not isinstance(key, str) or not isinstance(value, (str, int, float)):
            raise _ContractError("params must be dict[str, str | int | float]")
    return {"params": params, "answer_latex": answer}
'''

# runner 主体：exec 用户代码 → 调用 solve_kernel() → 校验契约 → 哨兵行回传。
_SOLVER_MAIN = '''
_ns = {"__name__": "sympy_sandbox", "__builtins__": _safe_builtins}
_real_stdout = _sys.stdout
# 用户 print 一律进 stderr：防伪造哨兵行，同时保留重试反馈素材
_sys.stdout = _sys.stderr
try:
    exec(compile(_USER_CODE, "<solve_kernel>", "exec"), _ns)
    _kernel = _ns.get("solve_kernel")
    if not callable(_kernel):
        raise _ContractError("solve_kernel() is not defined or not callable")
    _payload = _validate_contract(_kernel())
except _ContractError as exc:
    _sys.stdout = _real_stdout
    print(_ERROR_SENTINEL + " " + _json.dumps({"kind": "BAD_CONTRACT", "detail": str(exc)}))
    _sys.exit(0)
except BaseException:
    _sys.stdout = _real_stdout
    _traceback.print_exc()
    _sys.exit(1)
_sys.stdout = _real_stdout
print(_RESULT_SENTINEL + " " + _json.dumps(_payload, ensure_ascii=False))
'''


@dataclass
class _ProcOutcome:
    """隔离子进程的一次执行结果（内部 DTO）。"""

    timed_out: bool
    returncode: int | None
    stdout: str
    stderr: str


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    """start_new_session=True ⇒ 子进程为进程组组长（pgid == pid），强杀整个进程组。"""
    if hasattr(os, "killpg"):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _run_isolated(runner_code: str, timeout_s: float) -> _ProcOutcome:
    """等价于 subprocess.run([sys.executable, "-c", runner_code], capture_output=True,
    text=True, timeout=timeout_s, start_new_session=True)，但超时额外 os.killpg
    强杀整个进程组（subprocess.run 本身只杀直接子进程，会遗留孙进程）。"""
    proc = subprocess.Popen(
        [sys.executable, "-c", runner_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        stdout, stderr = proc.communicate()
        return _ProcOutcome(True, proc.returncode, stdout, stderr)
    return _ProcOutcome(False, proc.returncode, stdout, stderr)


def _stderr_tail(stderr: str, limit: int = _STDERR_TAIL_CHARS) -> str:
    return (stderr or "")[-limit:]


def _find_sentinel(stdout: str, sentinel: str) -> str | None:
    """取最后一条哨兵行的 JSON 载荷（runner 保证真实 stdout 上仅一条）。"""
    payload: str | None = None
    prefix = sentinel + " "
    for line in stdout.splitlines():
        if line.startswith(prefix):
            payload = line[len(prefix):].strip()
    return payload


def run_sympy_in_sandbox(
    code_str: str,
    timeout_s: float | None = None,
    max_mem_mb: int | None = None,
) -> SandboxExecutionResult:
    """AST 探针 → 平台兼容子进程执行 solve_kernel() → 契约校验。

    timeout_s / max_mem_mb 缺省取 settings.SYMPY_TIMEOUT_SECONDS / SYMPY_MAX_MEMORY_MB。
    要求 solve_kernel() 返回 {"params": dict[str, str|int|float], "answer_latex": 非空 str}。
    每次执行记录 sandbox.exec 行为事件（P5 观测层，不改任何执行行为）。
    """
    start = time.perf_counter()
    result = _run_sympy_in_sandbox_impl(code_str, timeout_s, max_mem_mb)
    log_event(
        logger,
        "sandbox.exec",
        mode="kernel",
        ok=result.ok,
        error_kind=result.error_kind,
        duration_ms=round((time.perf_counter() - start) * 1000, 2),
    )
    return result


def _run_sympy_in_sandbox_impl(
    code_str: str,
    timeout_s: float | None = None,
    max_mem_mb: int | None = None,
) -> SandboxExecutionResult:
    """沙盒执行实现（公开包装负责计时与 sandbox.exec 埋点）。"""
    probe = validate_script_ast(code_str)
    if not probe.ok:
        return SandboxExecutionResult(
            ok=False,
            params=None,
            answer_latex=None,
            error_kind="AST_REJECTED",
            stderr_tail=f"AST probe rejected: {probe.reason}",
        )

    timeout = settings.SYMPY_TIMEOUT_SECONDS if timeout_s is None else timeout_s
    mem = settings.SYMPY_MAX_MEMORY_MB if max_mem_mb is None else max_mem_mb
    runner_code = (
        _build_rlimit_preamble(mem, timeout)
        + _SOLVER_PREAMBLE
        + "_USER_CODE = "
        + json.dumps(code_str)
        + "\n"
        + _SOLVER_MAIN
    )
    outcome = _run_isolated(runner_code, timeout)
    tail = _stderr_tail(outcome.stderr)

    if outcome.timed_out:
        return SandboxExecutionResult(False, None, None, "TIMEOUT", tail)

    result_line = _find_sentinel(outcome.stdout, _RESULT_SENTINEL)
    if result_line is not None:
        try:
            data = json.loads(result_line)
            params = data["params"]
            answer = data["answer_latex"]
            params_ok = isinstance(params, dict) and all(
                isinstance(k, str) and isinstance(v, (str, int, float))
                for k, v in params.items()
            )
            if params_ok and isinstance(answer, str) and answer.strip():
                return SandboxExecutionResult(True, params, answer, None, tail)
        except (ValueError, KeyError, TypeError):
            pass
        return SandboxExecutionResult(
            False, None, None, "BAD_CONTRACT", tail or "malformed result sentinel"
        )

    error_line = _find_sentinel(outcome.stdout, _ERROR_SENTINEL)
    if error_line is not None:
        detail = error_line
        try:
            detail = str(json.loads(error_line).get("detail", error_line))
        except ValueError:
            pass
        return SandboxExecutionResult(False, None, None, "BAD_CONTRACT", _stderr_tail(detail))

    # 无哨兵：被 CPU 硬限/系统 SIGKILL 视为资源耗尽超时；其余按运行期错误归类
    if outcome.returncode in (-signal.SIGXCPU, -signal.SIGKILL):
        return SandboxExecutionResult(False, None, None, "TIMEOUT", tail)
    if outcome.returncode == 0:
        return SandboxExecutionResult(
            False, None, None, "BAD_CONTRACT", tail or "no result sentinel emitted"
        )
    return SandboxExecutionResult(False, None, None, "RUNTIME_ERROR", tail)


# ---------------------------------------------------------------------------
# 3. 代数等价引擎
# ---------------------------------------------------------------------------

# 多方程按 ; 或换行切分（设计 §3.3 第 1 条）
_EXPR_SPLIT_RE = re.compile(r"[;\n]+")
# 表达式字符黑名单：下划线（封死 __import__ 等内省）、引号/括号/反斜杠（封死
# parse_expr 内部 eval 的代码注入面）、其余非数学符号。数学表达式只需
# 字母/数字/空白与 + - * / ^ ( ) . , = 即可表达。
_FORBIDDEN_EXPR_CHARS = frozenset("_'\"\\[]{}#$@&|<>?:;`%!~")
_MAX_EXPR_CHARS = 4000

# 等价判定 runner：与沙盒共用 rlimit + 进程组超时隔离。
_EQUIV_RUNTIME = '''
import json as _json
import traceback as _traceback

from sympy import nsimplify as _nsimplify
from sympy import simplify as _simplify
from sympy.parsing.sympy_parser import convert_xor as _convert_xor
from sympy.parsing.sympy_parser import implicit_multiplication_application as _implicit_mul
from sympy.parsing.sympy_parser import parse_expr as _parse_expr
from sympy.parsing.sympy_parser import standard_transformations as _std_transforms

_TRANSFORMS = _std_transforms + (_implicit_mul, _convert_xor)


def _parse_equation(text):
    text = text.strip()
    if not text:
        return None
    if "=" in text:
        parts = text.split("=")
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            return None
        lhs = _parse_expr(parts[0].strip(), transformations=_TRANSFORMS)
        rhs = _parse_expr(parts[1].strip(), transformations=_TRANSFORMS)
        expr = lhs - rhs
    else:
        expr = _parse_expr(text, transformations=_TRANSFORMS)
    return _nsimplify(expr, rational=True)


def _single_equivalent(e1, e2):
    if e1 is None or e2 is None:
        return False
    try:
        if _simplify(e1 - e2) == 0:
            return True
        # PM 批准修复（用户裁决 2026-11-13）：反号/常数比率属方程缩放语义（e 与 -e、c·e 同解集），
        # 仅对含自由符号的表达式生效；纯数字答案要求严格数值相等（5≠7、5≠-5），
        # 否则任意两个非零常数互判等价会放行错误数值答案。
        if not (e1.free_symbols or e2.free_symbols):
            return False
        if _simplify(e1 + e2) == 0:
            return True
        if e2 == 0:
            return False
        ratio = _simplify(e1 / e2)
        if ratio.is_constant() and ratio != 0:
            return True
    except Exception:
        return False
    return False


_equivalent = False
try:
    _user_exprs = [_parse_equation(t) for t in _PAYLOAD["user"]]
    _std_exprs = [_parse_equation(t) for t in _PAYLOAD["standard"]]
    if (
        _user_exprs
        and _std_exprs
        and len(_user_exprs) == len(_std_exprs)
        and all(e is not None for e in _user_exprs)
        and all(e is not None for e in _std_exprs)
    ):
        _used = set()
        _equivalent = True
        for _ue in _user_exprs:
            for _i, _se in enumerate(_std_exprs):
                if _i not in _used and _single_equivalent(_ue, _se):
                    _used.add(_i)
                    break
            else:
                _equivalent = False
                break
except BaseException:
    _traceback.print_exc()
    _equivalent = False
print("###RESULT### " + _json.dumps({"equivalent": bool(_equivalent)}))
'''


def _split_equations(text: str) -> list[str]:
    return [part.strip() for part in _EXPR_SPLIT_RE.split(text) if part.strip()]


def _is_safe_expression(text: str) -> bool:
    if not text or len(text) > _MAX_EXPR_CHARS:
        return False
    if any(ch in _FORBIDDEN_EXPR_CHARS for ch in text):
        return False
    return all(ch.isprintable() or ch.isspace() for ch in text)


def verify_equation_equivalence(user_expr_str: str, standard_expr_str: str) -> bool:
    """半程立式拆拐杖的代数等价判据（设计 §3.3 第 1 条）。

    含 ``=`` 移项为 lhs - rhs；多方程按 ``;`` 或换行切分后逐一匹配（顺序无关）。
    单方程判据：simplify(e1-e2)==0 恒适用；反号 simplify(e1+e2)==0 与非零常数比率
    simplify(e1/e2).is_constant() 属方程缩放语义（e 与 -e、c·e 同解集），仅对含自由符号的
    表达式生效；纯数字答案要求严格数值相等（PM 批准修复：5≠7、5≠-5，防错误数值答案被放行）。
    解析失败 / 超时 / 任何异常一律返回 False，绝不向调用方抛异常。
    每次执行记录 sandbox.exec 行为事件（P5 观测层，不改任何判定行为）。
    """
    start = time.perf_counter()
    equivalent, error_kind = _verify_equation_equivalence_impl(user_expr_str, standard_expr_str)
    log_event(
        logger,
        "sandbox.exec",
        mode="equivalence",
        ok=error_kind is None,
        error_kind=error_kind,
        equivalent=equivalent,
        duration_ms=round((time.perf_counter() - start) * 1000, 2),
    )
    return equivalent


def _verify_equation_equivalence_impl(
    user_expr_str: str, standard_expr_str: str
) -> tuple[bool, str | None]:
    """等价判定实现：返回 (equivalent, error_kind)；error_kind=None 表示沙盒正常产出结论。"""
    try:
        if not isinstance(user_expr_str, str) or not isinstance(standard_expr_str, str):
            return False, "BAD_INPUT"
        user_eqs = _split_equations(user_expr_str)
        std_eqs = _split_equations(standard_expr_str)
        if not user_eqs or not std_eqs or len(user_eqs) != len(std_eqs):
            return False, "BAD_INPUT"
        if not all(_is_safe_expression(eq) for eq in user_eqs + std_eqs):
            return False, "UNSAFE_EXPRESSION"

        timeout = settings.SYMPY_TIMEOUT_SECONDS
        mem = settings.SYMPY_MAX_MEMORY_MB
        payload = json.dumps({"user": user_eqs, "standard": std_eqs})
        runner_code = (
            _build_rlimit_preamble(mem, timeout) + "_PAYLOAD = " + payload + "\n" + _EQUIV_RUNTIME
        )
        outcome = _run_isolated(runner_code, timeout)
        if outcome.timed_out:
            return False, "TIMEOUT"
        if outcome.returncode != 0:
            return False, "RUNTIME_ERROR"
        line = _find_sentinel(outcome.stdout, _RESULT_SENTINEL)
        if line is None:
            return False, "BAD_OUTPUT"
        try:
            return bool(json.loads(line)["equivalent"]), None
        except (ValueError, KeyError, TypeError):
            return False, "BAD_OUTPUT"
    except Exception:
        return False, "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# 4. LaTeX 答案规范化与等价判定共享栈（scaffold_engine / blind_drill_scheduler 共用）
# ---------------------------------------------------------------------------

_IMPLICATION_SEPS = ("\\Rightarrow", "\\Longrightarrow", "\\implies", "⇒", "→")
_TEXT_CMD_RE = re.compile(r"\\(?:text|mathrm|mbox|operatorname)\s*\{[^{}]*\}")
_FRAC_RE = re.compile(r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_SHORT_FRAC_RE = re.compile(
    r"\\frac\s*(?:\{([^{}]*)\}|([A-Za-z0-9]))\s*(?:\{([^{}]*)\}|([A-Za-z0-9]))"
)
_SQRT_NTH_RE = re.compile(r"\\sqrt\s*\[([^\[\]]+)\]\s*\{([^{}]*)\}")
_SQRT_RE = re.compile(r"\\sqrt\s*\{([^{}]*)\}")
_LEFT_RIGHT_RE = re.compile(r"\\(?:left|right)\b")
_SUB_BRACE_RE = re.compile(r"_\{([^{}]*)\}")
_SUP_BRACE_RE = re.compile(r"\^\{([^{}]*)\}")
_SUB_CHAR_RE = re.compile(r"_([A-Za-z0-9])")
_SPACING_RE = re.compile(r"\\[,;! ]")
_COMMAND_RE = re.compile(r"\\([A-Za-z]+)")

# ---- 单位/全角归一（PM 紧急修复 2026-11-13：带单位答案误判） ----

# 尾部中文单位短语表（模块级常量，可扩充）；最长优先匹配。
# 中文字符不可能是 SymPy 标识符，剥离尾部中文单位不影响代数语义。
_CN_UNIT_PHRASES: tuple[str, ...] = (
    "千米每小时", "平方厘米", "立方厘米", "米每秒", "米/秒",
    "平方米", "立方米", "厘米", "毫米", "分米", "千米", "公里",
    "分钟", "小时", "千克", "米", "秒", "天", "克", "吨", "元",
    "角", "个", "只", "条", "种", "度",
)
# 尾部单位 + 容忍尾随空白/。，,.（最长优先 alternation + $ 锚定）
_CN_UNIT_RE = re.compile(
    r"(?:" + "|".join(sorted(_CN_UNIT_PHRASES, key=len, reverse=True)) + r")[\s。，,.]*$"
)

# 拉丁单位豁免（仅当标准答案为纯数字时启用）：最长优先，尾随标点容忍。
# 前置 lookbehind 要求数字/空白：'15*m' 的 '*' 不满足 → 代数乘法绝不剥离。
_LATIN_UNIT_RE = re.compile(
    r"(?<=[\d\s])\s*(?:m\^3|m\^2|m/s|min|mm|cm|km|kg|m|g|t|h|s)[\s。，,.]*$"
)


def _fullwidth_to_halfwidth(text: str) -> str:
    """全角→半角：U+FF01~U+FF5E（数字/字母/＝（）．＋－／ 等）偏移 0xFEE0；全角空格→普通空格。"""
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        elif code == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def _strip_trailing_cn_unit(text: str) -> str:
    """剥离尾部中文单位短语（循环至不动点，容忍尾随空白/标点）。"""
    for _ in range(4):
        stripped = _CN_UNIT_RE.sub("", text).strip()
        if stripped == text:
            break
        text = stripped
    return text


def _is_pure_numeric(text: str) -> bool:
    """保守语法判定「parse 后无自由符号」：含数字、无字母、无 '='。

    无字母 ⇒ parse 不可能产生自由符号（宿主进程禁跑 parse_expr，安全红线）；
    比语义判定更严格（sqrt(2)/pi 等含字母的纯数不启用豁免），绝不误剥代数语境。
    """
    return (
        bool(text)
        and "=" not in text
        and re.search(r"[A-Za-z]", text) is None
        and any(ch.isdigit() for ch in text)
    )


# ---------------------------------------------------------------------------
# 复合数量解析与单位换算（2026-11-13 用户报告修复：「9小时36分」对
# 「9\ \text{小时}\ 36\ \text{分}」被误判——\text 剥离会把数字拼接成 "936"，
# 复合量必须在归一化之前解析换算）
# ---------------------------------------------------------------------------

# 单位 → (量纲, 换算到基本单位的因子)；时间基本单位=分，长度=米，质量=克，货币=元。
# 单字母拉丁单位（m/g/t/h/s）与代数符号同形，绝不进入数量解析——'15m' 对 '15*m'
# 必须保留隐式乘法语义（零漂移红线）；它们仅在权威路径裸值分支按量纲换算
# （_BARE_LATIN_UNIT_RE）。多字母拉丁单位（min/km/cm/mm/kg/dm）无歧义，保留。
_QUANTITY_UNITS: dict[str, tuple[str, float]] = {
    "小时": ("time", 60.0), "时": ("time", 60.0),
    "分钟": ("time", 1.0), "分": ("time", 1.0), "min": ("time", 1.0),
    "秒": ("time", 1.0 / 60.0),
    "千米": ("length", 1000.0), "公里": ("length", 1000.0), "km": ("length", 1000.0),
    "米": ("length", 1.0),
    "分米": ("length", 0.1), "dm": ("length", 0.1),
    "厘米": ("length", 0.01), "cm": ("length", 0.01),
    "毫米": ("length", 0.001), "mm": ("length", 0.001),
    "吨": ("mass", 1_000_000.0),
    "千克": ("mass", 1000.0), "kg": ("mass", 1000.0),
    "克": ("mass", 1.0),
    "元": ("money", 1.0), "角": ("money", 0.1),
}
_QUANTITY_UNIT_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*("
    + "|".join(sorted(_QUANTITY_UNITS, key=len, reverse=True))
    + r")"
)
# 基本单位值 → 各常用单位表示的乘数（裸数字答案按任一常用单位匹配）
_QUANTITY_REPS: dict[str, dict[str, float]] = {
    "time": {"小时": 1.0 / 60.0, "分": 1.0, "秒": 60.0},
    "length": {"千米": 1.0 / 1000.0, "米": 1.0, "厘米": 100.0, "毫米": 1000.0},
    "mass": {"吨": 1.0 / 1_000_000.0, "千克": 1.0 / 1000.0, "克": 1.0},
    "money": {"元": 1.0, "角": 10.0, "分": 100.0},
}
# 裸值分支的尾随单字母拉丁单位：全串「数字+单位」匹配（含运算符的 '15*m' 绝不匹配），
# 按量纲换算后与数量基本单位值比对——异量纲拒绝（'576s' 是 9.6 分，不是 576 分）。
_BARE_LATIN_UNIT_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(h|s|m|t|g)[\s。，,.]*$")
_BARE_LATIN_FACTORS: dict[str, tuple[str, float]] = {
    "h": ("time", 60.0), "s": ("time", 1.0 / 60.0),
    "m": ("length", 1.0),
    "t": ("mass", 1_000_000.0), "g": ("mass", 1.0),
}


def _num_eq(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


_CN_COUNTER_CHARS = "只个条张本名辆架台支根颗粒块片位户人岁套间"
_LABELED_ITEM_RE = re.compile(
    r"([A-Za-z\u4e00-\u9fff·_-]*[\u4e00-\u9fff][A-Za-z\u4e00-\u9fff·_\s-]*?)"
    r"\s*[:=]?\s*(-?\d+(?:\.\d+)?)\s*"
    rf"([{_CN_COUNTER_CHARS}]?)"
    r"(?=$|[\s,;、A-Za-z\u4e00-\u9fff])"
)
_BARE_NUMBER_LIST_RE = re.compile(
    r"^[\s,;、]*(-?\d+(?:\.\d+)?)(?:[\s,;、]+(-?\d+(?:\.\d+)?))*[\s,;、]*$"
)


def _unwrap_labeled_text(text: str) -> str:
    s = _fullwidth_to_halfwidth((text or "").strip())
    for sep in _IMPLICATION_SEPS:
        if sep in s:
            s = s.split(sep)[-1].strip()
    for _ in range(2):
        s = re.sub(r"\\(?:text|mathrm|mbox|operatorname)\s*\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\(?:qquad|quad|[,;:!])|\\ |~", " ", s)
    s = s.replace("$", "")
    return s.strip().rstrip("。，,.;；")


def _parse_labeled_numbers(text: str) -> list[tuple[str, float]] | None:
    """解析纯「含汉字实体标签 + 数值 + 可选计数词」序列，供多实体应用题比对。"""
    s = _unwrap_labeled_text(text)
    if not s or any(op in s for op in ("+", "*", "/", "^", "(", ")")):
        return None
    items: list[tuple[str, float]] = []
    pos = 0
    while pos < len(s):
        sep_match = re.match(r"[\s,;、]+", s[pos:])
        if sep_match is not None:
            pos += sep_match.end()
            continue
        m = _LABELED_ITEM_RE.match(s, pos)
        if m is None:
            return None
        label = re.sub(r"\s+", "", m.group(1))
        if len(label) > 1 and label[-1] in ("有", "为", "是"):
            label = label[:-1]
        if not any("\u4e00" <= ch <= "\u9fff" for ch in label):
            return None
        items.append((label, float(m.group(2))))
        pos = m.end()
    return items or None


def _labeled_numbers_equivalent(user_raw: str, std_items: list[tuple[str, float]]) -> bool:
    user_items = _parse_labeled_numbers(user_raw)
    if user_items is not None:
        if len(user_items) != len(std_items):
            return False
        u_sorted = sorted(user_items, key=lambda x: (x[0], x[1]))
        s_sorted = sorted(std_items, key=lambda x: (x[0], x[1]))
        return all(
            ul == sl and _num_eq(uv, sv)
            for (ul, uv), (sl, sv) in zip(u_sorted, s_sorted, strict=True)
        )
    if len(std_items) >= 2:
        s = _unwrap_labeled_text(user_raw)
        if _BARE_NUMBER_LIST_RE.fullmatch(s) is not None:
            nums = [float(tok) for tok in re.findall(r"-?\d+(?:\.\d+)?", s)]
            if len(nums) == len(std_items):
                return all(_num_eq(uv, sv) for uv, (_, sv) in zip(nums, std_items, strict=True))
    return False


def _parse_quantity(text: str) -> tuple[float, str] | None:
    """解析复合数量（纯「数字+单位」序列）→ (基本单位值, 量纲)。

    支持中文/ LaTeX 单位、全角、多段（如 9小时36分 / 2米30厘米）；
    非纯数量文本（方程/符号式/含运算符）返回 None，回落代数管线。
    「分」默认按时间（分钟）；仅当同串出现 元/角 时按货币（0.01元）。
    """
    s = _fullwidth_to_halfwidth((text or "").strip())
    if not s:
        return None
    # 与 normalize_answer 同步：蕴含链取结论段（否则结论为数量时解析被前缀破坏）
    for sep in _IMPLICATION_SEPS:
        if sep in s:
            s = s.split(sep)[-1].strip()
    s = re.sub(r"\\(?:text|mathrm|mbox|operatorname)\s*\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\(?:qquad|quad|[,;:!])|\\ |~", " ", s)
    s = re.sub(r"\s+", " ", s).strip().rstrip("。，,.")
    money_ctx = "元" in s or "角" in s
    pos = 0
    total = 0.0
    dim: str | None = None
    last_unit = ""
    matched = False
    while pos < len(s):
        m = _QUANTITY_UNIT_RE.match(s, pos)
        if m is not None:
            if matched and m.group(1).startswith("-"):
                return None  # 禁止后续分段出现负号（如 9小时-36分）
            num = float(m.group(1))
            unit = m.group(2)
            if unit == "分" and money_ctx:
                d, f = "money", 0.01
            else:
                d, f = _QUANTITY_UNITS[unit]
            if dim is None:
                dim = d
            elif d != dim:
                return None  # 混合量纲（如 9小时36米）非法
            total += num * f
            last_unit = unit
            matched = True
            pos = m.end()
            while pos < len(s) and s[pos] == " ":
                pos += 1
            continue
        # 「9小时36」：小时后尾部裸数字按分钟推断
        m2 = re.match(r"(\d+(?:\.\d+)?)$", s[pos:])
        if m2 is not None and matched and last_unit in ("小时", "时") and dim == "time":
            total += float(m2.group(1))
            pos = len(s)
            continue
        return None
    if not matched or dim is None:
        return None
    return total, dim


def _quantity_equivalent(
    qu: tuple[float, str] | None,
    qs: tuple[float, str] | None,
    user_raw: str,
    std_raw: str,
) -> bool:
    """数量等价（权威路径）：同量纲比基本单位值；裸数字按任一常用单位表示匹配。

    任一方解析为数量时本函数即最终判定，绝不回落 normalize 字符串比对
    （否则破损拼接值 "936" 会经归一化误判通过）。
    """
    if qu is not None and qs is not None:
        return qu[1] == qs[1] and _num_eq(qu[0], qs[0])
    qty, other_raw = (qs, user_raw) if qs is not None else (qu, std_raw)
    assert qty is not None
    other = normalize_answer(other_raw)
    if not other:
        return False
    # 尾随单字母拉丁单位（15m/576s）：按量纲换算，异量纲拒绝；
    # 全串匹配 → '15*m' 等代数式绝不进入数量换算（回落路径已先行排除）。
    lm = _BARE_LATIN_UNIT_RE.fullmatch(other)
    if lm is not None:
        d, f = _BARE_LATIN_FACTORS[lm.group(2)]
        return d == qty[1] and _num_eq(float(lm.group(1)) * f, qty[0])
    if not _is_pure_numeric(other):
        return False
    candidates = [qty[0] * f for f in _QUANTITY_REPS[qty[1]].values()]
    try:
        val = float(other)
    except ValueError:
        # 纯数字表达式（分数/幂/括号，无字母无 '='）：沙盒数值等价，
        # 保留旧管线对 '1152/2' 等写法的判定能力；仍不回落字符串比对。
        return any(verify_equation_equivalence(other, repr(c)) for c in candidates)
    return any(_num_eq(val, c) for c in candidates)


def _replace_short_frac(match: re.Match[str]) -> str:
    num = match.group(1) if match.group(1) is not None else match.group(2)
    den = match.group(3) if match.group(3) is not None else match.group(4)
    return f"(({num})/({den}))"


def latex_to_plain(text: str) -> str:
    """将步骤/答案 LaTeX 规范化为 verify_equation_equivalence 可解析的纯文本。

    去除单位（\\text{...}）、间距与 \\left/\\right；\\frac/\\dfrac/\\tfrac/\\sqrt 转函数式；
    下标扁平化（v_{max} → vmax，封死下划线内省面）；上标保留 ^（convert_xor）。
    """
    s = _TEXT_CMD_RE.sub("", text).replace(r"\dfrac", r"\frac").replace(r"\tfrac", r"\frac")
    for _ in range(8):  # 交替展开嵌套 \frac / \sqrt，直至不动点
        expanded = _SQRT_NTH_RE.sub(r"((\2)**(1/(\1)))", s)
        expanded = _SQRT_RE.sub(r"sqrt(\1)", expanded)
        expanded = _FRAC_RE.sub(r"((\1)/(\2))", expanded)
        expanded = _SHORT_FRAC_RE.sub(_replace_short_frac, expanded)
        if expanded == s:
            break
        s = expanded
    s = _LEFT_RIGHT_RE.sub("", s)
    s = _SUB_BRACE_RE.sub(r"\1", s)
    s = _SUP_BRACE_RE.sub(r"^(\1)", s)
    s = _SUB_CHAR_RE.sub(r"\1", s)
    s = s.replace(r"\cdot", "*").replace(r"\times", "*").replace(r"\div", "/")
    s = _SPACING_RE.sub("", s)
    s = s.replace("~", " ")
    s = _COMMAND_RE.sub(r"\1", s)  # 其余字母命令（\theta → theta 等）
    s = s.replace("{", "(").replace("}", ")").replace("\\", "").replace("_", "")
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"\s*=\s*", " = ", s).strip()


def normalize_answer(text: str) -> str:
    """规范化单条作答/标准式：全角归一 → 蕴含链取结论段 → LaTeX 归一 →
    链式等式 a=b=c 取首尾 a=c → 剥离尾部中文单位（对双方生效）。"""
    s = _fullwidth_to_halfwidth((text or "").strip())
    for sep in _IMPLICATION_SEPS:
        if sep in s:
            s = s.split(sep)[-1].strip()
    s = latex_to_plain(s)
    if s.count("=") > 1 and ";" not in s and "\n" not in s:
        parts = [p.strip() for p in s.split("=")]
        s = f"{parts[0]} = {parts[-1]}"
    s = _strip_trailing_cn_unit(s)
    return s.strip()


def answers_equivalent(user_answer: str, standard: str) -> bool:
    """代数等价判定：复合数量换算（权威快路径）→ 规范化字符串比对 →
    拉丁单位豁免（仅纯数字标准答案）→ SymPy 沙盒等价 → 裸值对单方程右端。"""
    # 复合数量快路径：必须在 normalize 之前解析双方原始文本——\text 剥离会把
    # "9\text{小时}36\text{分}" 拼接成 "936"，归一化后不可逆；任一方为纯数量时
    # 本路径为权威判定（绝不回落破损拼接比对，"936" 不得通过）。
    qu = _parse_quantity(user_answer)
    qs = _parse_quantity(standard)
    if qs is not None or qu is not None:
        return _quantity_equivalent(qu, qs, user_answer, standard)
    std_labeled = _parse_labeled_numbers(standard)
    if std_labeled is not None and _labeled_numbers_equivalent(user_answer, std_labeled):
        return True
    user = normalize_answer(user_answer)
    std = normalize_answer(standard)
    if not user or not std:
        return False
    if user == std:
        return True
    # 拉丁单位豁免：仅当标准答案为纯数字（无自由符号且不含 '='）时剥离用户答案
    # 尾部拉丁单位；标准答案含符号时绝不剥离（代数语境 '15m'=15*m 必须保留）。
    if _is_pure_numeric(std):
        stripped = _LATIN_UNIT_RE.sub("", user).strip()
        if stripped and stripped != user:
            if stripped == std:
                return True
            user = stripped
    if verify_equation_equivalence(user, std):
        return True
    # 标准答案为单方程 var=value 时，允许用户仅提交裸值（如 "20" 对 "vmax = 20"）
    if "=" not in user and std.count("=") == 1:
        rhs = std.split("=", 1)[1].strip()
        if rhs and (user == rhs or verify_equation_equivalence(user, rhs)):
            return True
    return False
