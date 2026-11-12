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
  子进程内执行并带超时保护（parse_expr 内部会 eval，绝不在宿主进程执行）。

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
import math
import os
import re
import signal
import subprocess
import sys
import textwrap
from dataclasses import dataclass

from backend.config import settings

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
    """
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
    单方程判据（任一成立即等价）：simplify(e1-e2)==0、simplify(e1+e2)==0（相反数）、
    simplify(e1/e2).is_constant() 且非零（常数比率）。
    解析失败 / 超时 / 任何异常一律返回 False，绝不向调用方抛异常。
    """
    try:
        if not isinstance(user_expr_str, str) or not isinstance(standard_expr_str, str):
            return False
        user_eqs = _split_equations(user_expr_str)
        std_eqs = _split_equations(standard_expr_str)
        if not user_eqs or not std_eqs or len(user_eqs) != len(std_eqs):
            return False
        if not all(_is_safe_expression(eq) for eq in user_eqs + std_eqs):
            return False

        timeout = settings.SYMPY_TIMEOUT_SECONDS
        mem = settings.SYMPY_MAX_MEMORY_MB
        payload = json.dumps({"user": user_eqs, "standard": std_eqs})
        runner_code = (
            _build_rlimit_preamble(mem, timeout) + "_PAYLOAD = " + payload + "\n" + _EQUIV_RUNTIME
        )
        outcome = _run_isolated(runner_code, timeout)
        if outcome.timed_out or outcome.returncode != 0:
            return False
        line = _find_sentinel(outcome.stdout, _RESULT_SENTINEL)
        if line is None:
            return False
        return bool(json.loads(line)["equivalent"])
    except Exception:
        return False
