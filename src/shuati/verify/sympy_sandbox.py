"""跨平台 SymPy 验算沙盒：AST 静态探针（反空壳）+ 子进程隔离（Windows Job Object / Unix rlimit）。"""

from __future__ import annotations

import ast
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from typing import Any, Literal

_ALLOWED_IMPORT_ROOTS = frozenset({"sympy", "math", "fractions"})
_FORBIDDEN_CALL_NAMES = frozenset(
    {
        "open",
        "eval",
        "exec",
        "compile",
        "__import__",
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
        "breakpoint",
        "input",
        "memoryview",
        "sympify",
        "parse_expr",
        "eval_expr",
        "stringify_expr",
        "lambdify",
        "parse_latex",
        "parse_mathematica",
        "import_module",
        "autowrap",
        "ufuncify",
    }
)
_FORBIDDEN_NAMES = frozenset(
    {
        "sys",
        "os",
        "subprocess",
        "importlib",
        "builtins",
        "ctypes",
        "socket",
        "pathlib",
        "shutil",
        "pickle",
        "marshal",
        "resource",
        "signal",
        "threading",
        "multiprocessing",
        "inspect",
        "io",
        "parsing",
        "sympy_parser",
        "external",
        "utilities",
        "interactive",
        "testing",
        "benchmarks",
        "codegen",
        "codeprinter",
        "importtools",
        "posix",
        "nt",
    }
)
_FORBIDDEN_ATTR_NAMES = frozenset(
    {
        "sys",
        "os",
        "subprocess",
        "importlib",
        "builtins",
        "ctypes",
        "socket",
        "pathlib",
        "shutil",
        "io",
        "modules",
        "system",
        "popen",
        "spawn",
        "fork",
        "execv",
        "execl",
        "execve",
        "environ",
        "read_text",
        "write_text",
        "read_bytes",
        "write_bytes",
        "open",
        "eval",
        "exec",
        "compile",
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
        "breakpoint",
        "sympify",
        "parse_expr",
        "eval_expr",
        "stringify_expr",
        "lambdify",
        "parse_latex",
        "parse_mathematica",
        "parsing",
        "sympy_parser",
        "external",
        "utilities",
        "interactive",
        "testing",
        "benchmarks",
        "codegen",
        "codeprinter",
        "import_module",
        "importtools",
        "autowrap",
        "ufuncify",
        "cython",
        "posix",
        "nt",
    }
)

_SAFE_STRING_CALL_NAMES = frozenset(
    {
        "Symbol",
        "symbols",
        "Dummy",
        "Wild",
        "Function",
        "var",
        "print",
        "format",
        "str",
        "repr",
        "int",
        "float",
        "dict",
        "list",
        "tuple",
        "set",
        "len",
        "isinstance",
        "sorted",
        "join",
        "split",
        "replace",
        "strip",
        "lstrip",
        "rstrip",
        "upper",
        "lower",
        "startswith",
        "endswith",
        "get",
        "setdefault",
        "pop",
        "update",
        "append",
        "extend",
        "insert",
        "index",
        "count",
    }
)

_TRIVIAL_WRAPPER_CALLS = frozenset({"str", "repr", "int", "float", "list", "tuple", "set", "dict"})

_RESULT_SENTINEL = "###RESULT###"
_ERROR_SENTINEL = "###ERROR###"
_STDERR_TAIL_CHARS = 800


@dataclass
class ASTValidationResult:
    ok: bool
    reason: str  # "OK" | "SYNTAX_ERROR" | "FORBIDDEN_IMPORT" | "FORBIDDEN_CALL" | "FORBIDDEN_ATTR" | "FORBIDDEN_NAME" | "VACUOUS_SCRIPT_REJECTED"


@dataclass
class SympySandboxResult:
    ok: bool
    status: Literal["PASSED", "FAILED", "TIMEOUT", "AST_REJECTED"]
    values: dict[str, str | list[str]] | None
    stderr_tail: str = ""


# ---------------------------------------------------------------------------
# 1. AST 静态探针（安全拦截 + 反空壳校验）
# ---------------------------------------------------------------------------


def _import_name_allowed(name: str) -> bool:
    parts = name.split(".")
    return parts[0] in _ALLOWED_IMPORT_ROOTS and not any(
        p.startswith("_") or p in _FORBIDDEN_ATTR_NAMES or p in _FORBIDDEN_NAMES for p in parts
    )


def _check_forbidden_name(ident: str) -> str | None:
    if ident in _FORBIDDEN_CALL_NAMES:
        return "FORBIDDEN_CALL"
    if ident in _FORBIDDEN_NAMES:
        return "FORBIDDEN_NAME"
    if ident.startswith("__"):
        return "FORBIDDEN_ATTR"
    return None


def _check_forbidden_attr(attr: str) -> str | None:
    if attr.startswith("_"):
        return "FORBIDDEN_ATTR"
    if attr in _FORBIDDEN_CALL_NAMES:
        return "FORBIDDEN_CALL"
    if attr in _FORBIDDEN_ATTR_NAMES:
        return "FORBIDDEN_ATTR"
    return None


def _is_numeric_literal_ast(node: ast.AST) -> bool:
    """判断 AST 节点是否为纯整数/浮点字面量（含一元 +/- 号），用于放行 S(3) / S(-1)。"""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        return _is_numeric_literal_ast(node.operand)
    return False


def _is_s_call_allowed(call_node: ast.Call) -> bool:
    """允许 S(3) 等纯数值字面量调用；拦截 S(<字符串常量或变量>)。"""
    if call_node.keywords or len(call_node.args) != 1:
        return False
    return _is_numeric_literal_ast(call_node.args[0])


def _is_pure_literal_ast(node: ast.AST) -> bool:
    """判断表达式本身是否仅由常量字面量构成。"""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub, ast.Not)):
        return _is_pure_literal_ast(node.operand)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_pure_literal_ast(elt) for elt in node.elts)
    if isinstance(node, ast.Dict):
        return all(_is_pure_literal_ast(v) for v in node.values)
    if isinstance(node, ast.JoinedStr):
        return all(
            isinstance(v, ast.Constant) or (isinstance(v, ast.FormattedValue) and _is_pure_literal_ast(v.value))
            for v in node.values
        )
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _TRIVIAL_WRAPPER_CALLS:
        return not node.keywords and all(_is_pure_literal_ast(a) for a in node.args)
    return False


_NON_NUMERIC_FALLBACK_SENTINELS = frozenset(
    {
        "",
        "无解",
        "无实数解",
        "无实根",
        "无",
        "不存在",
        "无法确定",
        "n/a",
        "na",
        "none",
        "null",
        "nil",
        "empty",
        "undefined",
        "no solution",
        "no_solution",
        "no real solution",
    }
)


def _is_non_numeric_fallback_sentinel(node: ast.AST) -> bool:
    """判断 AST 节点是否为明确的空值/无解兜底哨兵字面量，拒绝数值及单字母选项等任意字符串。"""
    if not isinstance(node, ast.Constant):
        return False
    if node.value is None:
        return True
    if not isinstance(node.value, str):
        return False
    return node.value.strip().lower() in _NON_NUMERIC_FALLBACK_SENTINELS


def _is_computed_expr(expr: ast.AST, computed_vars: dict[str, bool]) -> bool:
    if _is_pure_literal_ast(expr):
        return False
    if isinstance(expr, ast.Name):
        return computed_vars.get(expr.id, False)
    if isinstance(expr, ast.UnaryOp):
        return _is_computed_expr(expr.operand, computed_vars)
    if isinstance(expr, ast.BinOp):
        # 若任一侧是变量/调用/运算符，视为经由算术运算得出；纯字面量运算如 6 * 7 亦为 BinOp 真实算术计算
        return True
    if isinstance(expr, ast.BoolOp):
        return any(_is_computed_expr(v, computed_vars) for v in expr.values)
    if isinstance(expr, ast.Compare):
        return _is_computed_expr(expr.left, computed_vars) or any(
            _is_computed_expr(comp, computed_vars) for comp in expr.comparators
        )
    if isinstance(expr, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        return True
    if isinstance(expr, ast.JoinedStr):
        return any(
            isinstance(v, ast.FormattedValue) and _is_computed_expr(v.value, computed_vars)
            for v in expr.values
        )
    if isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
        return bool(expr.elts) and all(_is_computed_expr(elt, computed_vars) for elt in expr.elts)
    if isinstance(expr, ast.Call):
        if isinstance(expr.func, ast.Name) and expr.func.id in _TRIVIAL_WRAPPER_CALLS:
            return any(_is_computed_expr(a, computed_vars) for a in expr.args) or any(
                _is_computed_expr(kw.value, computed_vars) for kw in expr.keywords
            )
        return True
    if isinstance(expr, ast.IfExp):
        body_comp = _is_computed_expr(expr.body, computed_vars)
        orelse_comp = _is_computed_expr(expr.orelse, computed_vars)
        if body_comp and orelse_comp:
            return True
        return (
            body_comp
            and _is_computed_expr(expr.test, computed_vars)
            and _is_non_numeric_fallback_sentinel(expr.orelse)
        )
    if isinstance(expr, ast.Subscript):
        return _is_computed_expr(expr.value, computed_vars)
    if isinstance(expr, ast.Dict):
        return bool(expr.values) and all(_is_computed_expr(v, computed_vars) for v in expr.values)
    if isinstance(expr, ast.Attribute):
        return True
    return True


def _collect_var_state(stmts: list[ast.stmt]) -> tuple[dict[str, bool], dict[str, ast.Dict], dict[str, list[ast.expr]]]:
    """收集语句块内的变量计算属性（is_computed）、字面字典绑定及下标赋值值列表。"""
    computed_vars: dict[str, bool] = {}
    dict_bindings: dict[str, ast.Dict] = {}
    dict_subscripts: dict[str, list[ast.expr]] = {}

    def _mark_target(target: ast.AST, is_comp: bool, val_expr: ast.AST | None = None) -> None:
        if isinstance(target, ast.Name):
            computed_vars[target.id] = computed_vars.get(target.id, False) or is_comp
            if isinstance(val_expr, ast.Dict):
                dict_bindings[target.id] = val_expr
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                _mark_target(elt, is_comp, None)

    def _walk_stmt_list(body: list[ast.stmt]) -> None:
        for st in body:
            if isinstance(st, ast.Assign):
                comp = _is_computed_expr(st.value, computed_vars)
                for tgt in st.targets:
                    if isinstance(tgt, ast.Subscript) and isinstance(tgt.value, ast.Name):
                        dict_subscripts.setdefault(tgt.value.id, []).append(st.value)
                        computed_vars[tgt.value.id] = computed_vars.get(tgt.value.id, False) or comp
                    else:
                        _mark_target(tgt, comp, st.value)
            elif isinstance(st, ast.AnnAssign) and st.value is not None:
                comp = _is_computed_expr(st.value, computed_vars)
                if isinstance(st.target, ast.Subscript) and isinstance(st.target.value, ast.Name):
                    dict_subscripts.setdefault(st.target.value.id, []).append(st.value)
                else:
                    _mark_target(st.target, comp, st.value)
            elif isinstance(st, ast.AugAssign):
                if isinstance(st.target, ast.Name):
                    computed_vars[st.target.id] = True
                elif isinstance(st.target, ast.Subscript) and isinstance(st.target.value, ast.Name):
                    dict_subscripts.setdefault(st.target.value.id, []).append(st.value)
            elif isinstance(st, (ast.For, ast.AsyncFor)):
                iter_comp = _is_computed_expr(st.iter, computed_vars)
                _mark_target(st.target, iter_comp, None)
                _walk_stmt_list(st.body)
                _walk_stmt_list(st.orelse)
            elif isinstance(st, ast.While):
                _walk_stmt_list(st.body)
                _walk_stmt_list(st.orelse)
            elif isinstance(st, ast.If):
                _walk_stmt_list(st.body)
                _walk_stmt_list(st.orelse)
            elif isinstance(st, (ast.With, ast.AsyncWith)):
                for item in st.items:
                    if item.optional_vars is not None:
                        _mark_target(item.optional_vars, True, None)
                _walk_stmt_list(st.body)
            elif isinstance(st, ast.Try):
                _walk_stmt_list(st.body)
                for h in st.handlers:
                    _walk_stmt_list(h.body)
                _walk_stmt_list(st.orelse)
                _walk_stmt_list(st.finalbody)

    _walk_stmt_list(stmts)
    return computed_vars, dict_bindings, dict_subscripts


def _validate_verify_anti_tautology(tree: ast.Module) -> bool:
    """检查必须存在 def verify() 且返回字典中每个值均经过真实运算，而非纯常量空壳。"""
    verify_fn: ast.FunctionDef | None = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "verify":
            verify_fn = node
            break
    if verify_fn is None:
        return False

    # 收集模块级与 verify 内部变量计算状态
    mod_computed, mod_dicts, mod_subs = _collect_var_state(
        [st for st in tree.body if not isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    )
    fn_computed, fn_dicts, fn_subs = _collect_var_state(verify_fn.body)
    computed_vars = {**mod_computed, **fn_computed}
    dict_bindings = {**mod_dicts, **fn_dicts}
    dict_subs: dict[str, list[ast.expr]] = {k: list(v) for k, v in mod_subs.items()}
    for k, v in fn_subs.items():
        dict_subs.setdefault(k, []).extend(v)

    # 收集 verify 自身（不含内部嵌套 def）的所有 return
    returns: list[ast.Return] = []

    def _collect_returns(stmts: list[ast.stmt]) -> None:
        for st in stmts:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(st, ast.Return):
                returns.append(st)
            for child in ast.iter_child_nodes(st):
                if isinstance(child, ast.stmt) and not isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    _collect_returns([child])

    _collect_returns(verify_fn.body)
    if not returns:
        return False

    for ret in returns:
        val = ret.value
        if val is None:
            return False
        if isinstance(val, ast.Dict):
            if not val.values:
                return False
            for dv in val.values:
                if not _is_computed_expr(dv, computed_vars):
                    return False
        elif isinstance(val, ast.DictComp):
            if not _is_computed_expr(val.value, computed_vars):
                return False
        elif isinstance(val, ast.Name):
            var_name = val.id
            has_dict_lit = var_name in dict_bindings
            sub_vals = dict_subs.get(var_name, [])
            if not has_dict_lit and not sub_vals and not computed_vars.get(var_name, False):
                return False
            if has_dict_lit:
                d_lit = dict_bindings[var_name]
                if not d_lit.values and not sub_vals:
                    return False
                for dv in d_lit.values:
                    if not _is_computed_expr(dv, computed_vars):
                        return False
            for sv in sub_vals:
                if not _is_computed_expr(sv, computed_vars):
                    return False
        elif isinstance(val, ast.Call):
            if isinstance(val.func, ast.Name) and val.func.id == "dict":
                if not val.args and not val.keywords:
                    return False
                if any(not _is_computed_expr(kw.value, computed_vars) for kw in val.keywords):
                    return False
        else:
            return False

    return True


def _collect_string_tainted_vars(tree: ast.Module) -> set[str]:
    str_vars: set[str] = set()

    def _is_str_expr(expr: ast.AST) -> bool:
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return True
        if isinstance(expr, ast.JoinedStr):
            return True
        if isinstance(expr, ast.Name) and expr.id in str_vars:
            return True
        if isinstance(expr, ast.BinOp):
            return _is_str_expr(expr.left) or _is_str_expr(expr.right)
        if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id in {"str", "repr", "format"}:
            return True
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_str_expr(node.value):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    str_vars.add(tgt.id)
        elif isinstance(node, ast.AnnAssign) and node.value is not None and _is_str_expr(node.value):
            if isinstance(node.target, ast.Name):
                str_vars.add(node.target.id)
    return str_vars


def _call_uses_string_arg(call_node: ast.Call, str_vars: set[str]) -> bool:
    for arg in call_node.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return True
        if isinstance(arg, ast.JoinedStr):
            return True
        if isinstance(arg, ast.Name) and arg.id in str_vars:
            return True
    for kw in call_node.keywords:
        if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return True
        if isinstance(kw.value, ast.JoinedStr):
            return True
        if isinstance(kw.value, ast.Name) and kw.value.id in str_vars:
            return True
    return False


def validate_script_ast(code_str: str) -> ASTValidationResult:
    """AST 静态探针：白名单导入 + 危险 API/内省/字符串求值拦截 + 反空壳校验。"""
    if not isinstance(code_str, str) or not code_str.strip():
        return ASTValidationResult(False, "SYNTAX_ERROR")
    try:
        tree = ast.parse(code_str)
    except (SyntaxError, ValueError):
        return ASTValidationResult(False, "SYNTAX_ERROR")

    user_fn_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    str_vars = _collect_string_tainted_vars(tree)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _import_name_allowed(alias.name):
                    return ASTValidationResult(False, "FORBIDDEN_IMPORT")
                if alias.asname is not None:
                    reason = _check_forbidden_name(alias.asname)
                    if reason is not None:
                        return ASTValidationResult(False, reason)
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0 or node.module is None or not _import_name_allowed(node.module):
                return ASTValidationResult(False, "FORBIDDEN_IMPORT")
            for alias in node.names:
                if alias.name != "*":
                    if alias.name.startswith("_"):
                        return ASTValidationResult(False, "FORBIDDEN_IMPORT")
                    reason = _check_forbidden_attr(alias.name) or _check_forbidden_name(alias.name)
                    if reason is not None:
                        return ASTValidationResult(False, reason)
                if alias.asname is not None:
                    as_reason = _check_forbidden_name(alias.asname)
                    if as_reason is not None:
                        return ASTValidationResult(False, as_reason)
        elif isinstance(node, ast.Name):
            reason = _check_forbidden_name(node.id)
            if reason is not None:
                return ASTValidationResult(False, reason)
        elif isinstance(node, ast.Attribute):
            reason = _check_forbidden_attr(node.attr)
            if reason is not None:
                return ASTValidationResult(False, reason)
        elif isinstance(node, ast.Call):
            func = node.func
            call_name: str | None = None
            if isinstance(func, ast.Name):
                reason = _check_forbidden_name(func.id)
                if reason is not None:
                    return ASTValidationResult(False, reason)
                if func.id == "S" and not _is_s_call_allowed(node):
                    return ASTValidationResult(False, "FORBIDDEN_CALL")
                call_name = func.id
            elif isinstance(func, ast.Attribute):
                reason = _check_forbidden_attr(func.attr)
                if reason is not None:
                    return ASTValidationResult(False, reason)
                if func.attr == "S" and not _is_s_call_allowed(node):
                    return ASTValidationResult(False, "FORBIDDEN_CALL")
                call_name = func.attr

            if call_name is not None and call_name not in _SAFE_STRING_CALL_NAMES and call_name not in user_fn_names:
                if _call_uses_string_arg(node, str_vars):
                    return ASTValidationResult(False, "FORBIDDEN_CALL")

    if not _validate_verify_anti_tautology(tree):
        return ASTValidationResult(False, "VACUOUS_SCRIPT_REJECTED")

    return ASTValidationResult(True, "OK")


# ---------------------------------------------------------------------------
# 2. 跨平台子进程隔离执行（Windows Job Object / Unix rlimit）
# ---------------------------------------------------------------------------


def _build_rlimit_preamble(memory_limit_mb: int, timeout_seconds: float) -> str:
    if sys.platform == "win32":
        return ""
    cpu_limit = math.ceil(timeout_seconds) + 1
    mem_bytes = int(memory_limit_mb) * 1024 * 1024
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
            try:
                _resource.setrlimit(_resource.RLIMIT_AS, (_MEM_BYTES, _MEM_BYTES))
            except (ValueError, OSError):
                pass
        """)


_VERIFY_PREAMBLE = '''
import builtins as _builtins
import importlib as _importlib
import json as _json
import re as _re
import sys as _sys
import traceback as _traceback

_RESULT_SENTINEL = "###RESULT###"
_ERROR_SENTINEL = "###ERROR###"
_ALLOWED_ROOTS = ("sympy", "math", "fractions")
_FORBIDDEN_IMPORT_PARTS = frozenset(
    {
        "sys", "os", "subprocess", "importlib", "builtins", "ctypes",
        "socket", "pathlib", "shutil", "io", "pickle", "marshal",
        "resource", "signal", "threading", "multiprocessing", "inspect",
        "parsing", "sympy_parser", "external", "utilities", "interactive",
        "testing", "benchmarks", "codegen", "codeprinter", "importtools",
        "autowrap", "ufuncify", "cython", "posix", "nt",
    }
)
_HARD_BLOCKED_ROOTS = frozenset(
    {
        "os", "subprocess", "ctypes", "socket", "pathlib", "shutil",
        "pickle", "marshal", "threading", "multiprocessing", "resource",
        "signal", "runpy", "webbrowser", "urllib", "http", "ftplib",
        "smtplib", "poplib", "imaplib", "telnetlib", "ssl", "sqlite3",
        "posix", "nt", "winreg", "msvcrt", "fcntl", "pty", "pdb",
        "code", "codeop", "venv", "ensurepip", "pip", "builtins",
    }
)
_SYMPY_BLOCKED_SUBMODULES = frozenset(
    {
        "external", "interactive", "testing", "benchmarks",
        "codegen", "codeprinter", "parsing", "sympy_parser",
    }
)
_SYMPY_INTERNAL_ALLOWED_EXTERNAL = frozenset(
    {
        "sympy.external.gmpy",
        "sympy.external.pythonmpq",
    }
)
_real_import = _builtins.__import__


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    parts = name.split(".")
    if level != 0 or parts[0] not in _ALLOWED_ROOTS:
        raise ImportError("sandbox: forbidden import " + repr(name))
    if any(p.startswith("_") or p in _FORBIDDEN_IMPORT_PARTS for p in parts):
        raise ImportError("sandbox: forbidden import " + repr(name))
    if fromlist and any(
        isinstance(f, str) and (f.startswith("_") or f in _FORBIDDEN_IMPORT_PARTS)
        for f in fromlist
    ):
        raise ImportError("sandbox: forbidden import " + repr(name))
    return _real_import(name, globals, locals, fromlist, level)


def _global_safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    caller_name = globals.get("__name__", "") if isinstance(globals, dict) else ""
    is_sympy_internal = (
        isinstance(globals, dict)
        and caller_name.startswith("sympy.")
        and caller_name != "sympy_sandbox"
    )
    parts = name.split(".")
    if level == 0:
        if parts[0] in _HARD_BLOCKED_ROOTS:
            raise ImportError("sandbox: forbidden global import " + repr(name))
        if is_sympy_internal and name in _SYMPY_INTERNAL_ALLOWED_EXTERNAL:
            if fromlist and any(
                isinstance(f, str)
                and (f.startswith("_") or f in _FORBIDDEN_IMPORT_PARTS or f == "import_module")
                for f in fromlist
            ):
                raise ImportError("sandbox: forbidden sympy submodule " + repr(name))
            return _real_import(name, globals, locals, fromlist, level)
        if parts[0] == "sympy" and any(p in _SYMPY_BLOCKED_SUBMODULES for p in parts[1:]):
            raise ImportError("sandbox: forbidden sympy submodule " + repr(name))
    else:
        if any(p in _HARD_BLOCKED_ROOTS or p in _SYMPY_BLOCKED_SUBMODULES for p in parts):
            raise ImportError("sandbox: forbidden relative import " + repr(name))

    if not isinstance(globals, dict) or caller_name == "sympy_sandbox":
        return _safe_import(name, globals, locals, fromlist, level)

    return _real_import(name, globals, locals, fromlist, level)


# 纵深防御：预加载并锁定子进程内 sympy 字符串求值、动态导入入口与全局 builtins
_sympy_mod = _importlib.import_module("sympy")
_sympy_parser_mod = _importlib.import_module("sympy.parsing.sympy_parser")
_sympy_sympify_mod = _importlib.import_module("sympy.core.sympify")
_sympy_ext_mod = _importlib.import_module("sympy.external")
_sympy_ext_tools_mod = _importlib.import_module("sympy.external.importtools")
_sympy_ext_gmpy_mod = _importlib.import_module("sympy.external.gmpy")
_sympy_ext_pythonmpq_mod = _importlib.import_module("sympy.external.pythonmpq")
_orig_sympify = _sympy_sympify_mod.sympify


def _blocked_eval(*_args, **_kwargs):
    raise PermissionError("sandbox: string eval is disabled")


def _blocked_import_module(*_args, **_kwargs):
    raise ImportError("sandbox: import_module is disabled")


def _safe_sympy_parse_expr(s, *args, **kwargs):
    if isinstance(s, str) and _re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", s):
        return _sympy_mod.Symbol(s)
    raise PermissionError("sandbox: parse_expr is disabled")


def _safe_sympy_sympify(a, *args, **kwargs):
    if isinstance(a, str):
        if _re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", a):
            return _sympy_mod.Symbol(a)
        raise PermissionError("sandbox: string sympify is disabled")
    return _orig_sympify(a, *args, **kwargs)


_sympy_parser_mod.eval_expr = _blocked_eval
_sympy_parser_mod.stringify_expr = _blocked_eval
_sympy_parser_mod.parse_expr = _safe_sympy_parse_expr
_sympy_sympify_mod.sympify = _safe_sympy_sympify
_sympy_ext_mod.import_module = _blocked_import_module
_sympy_ext_tools_mod.import_module = _blocked_import_module
if hasattr(_sympy_ext_gmpy_mod, "import_module"):
    _sympy_ext_gmpy_mod.import_module = _blocked_import_module


def _blocked_builtin(name):
    def _raiser(*_args, **_kwargs):
        raise PermissionError("sandbox: builtin " + name + " is disabled")
    return _raiser


_builtins.open = _blocked_builtin("open")
_builtins.input = _blocked_builtin("input")
_builtins.breakpoint = _blocked_builtin("breakpoint")
_builtins.__import__ = _global_safe_import

_DROP_BUILTINS = {
    "open", "eval", "exec", "compile", "getattr", "setattr", "delattr",
    "globals", "locals", "vars", "dir", "input", "breakpoint", "help",
    "exit", "quit", "memoryview", "__import__",
}
_safe_builtins = {k: v for k, v in vars(_builtins).items() if k not in _DROP_BUILTINS}
_safe_builtins["__import__"] = _safe_import


class _ContractError(Exception):
    pass


def _convert_verify_result(result):
    if not isinstance(result, dict) or not result:
        raise _ContractError("verify() must return a non-empty dict")
    converted = {}
    for k, v in result.items():
        if not isinstance(k, str):
            raise _ContractError("verify() keys must be str")
        if isinstance(v, (list, tuple)):
            converted[k] = [str(item) for item in v]
        elif v is None or isinstance(v, (dict, bool)):
            raise _ContractError("verify() values must be scalar or list[str]")
        else:
            converted[k] = str(v)
    return converted
'''

_VERIFY_MAIN = '''
_ns = {"__name__": "sympy_sandbox", "__builtins__": _safe_builtins}
_real_stdout = _sys.stdout
_sys.stdout = _sys.stderr
try:
    exec(compile(_USER_CODE, "<verify_script>", "exec"), _ns)
    _fn = _ns.get("verify")
    if not callable(_fn):
        raise _ContractError("verify() is not defined or not callable")
    _payload = _convert_verify_result(_fn())
except _ContractError as exc:
    _sys.stdout = _real_stdout
    print(_ERROR_SENTINEL + " " + _json.dumps({"kind": "BAD_CONTRACT", "detail": str(exc)}, ensure_ascii=False))
    _sys.exit(0)
except BaseException:
    _sys.stdout = _real_stdout
    _traceback.print_exc()
    _sys.exit(1)
_sys.stdout = _real_stdout
print(_RESULT_SENTINEL + " " + _json.dumps(_payload, ensure_ascii=False))
'''


def _minimal_env() -> dict[str, str]:
    keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _create_windows_job_object(memory_limit_mb: int) -> Any:
    """创建并配置 Windows Job Object（内存上限 + 关闭句柄即强杀进程树）。"""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    JOB_OBJECT_EXTENDED_LIMIT_INFO = 9
    JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    h_job = kernel32.CreateJobObjectW(None, None)
    if not h_job:
        return None

    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = (
        JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    info.ProcessMemoryLimit = int(memory_limit_mb) * 1024 * 1024

    ok = kernel32.SetInformationJobObject(
        h_job,
        JOB_OBJECT_EXTENDED_LIMIT_INFO,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        kernel32.CloseHandle(h_job)
        return None

    return (kernel32, h_job)


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    if sys.platform == "win32":
        try:
            res = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
                timeout=5.0,
            )
            if res.returncode == 0:
                return
        except Exception:
            pass
        try:
            proc.kill()
        except OSError:
            pass
    else:
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


@dataclass
class _ProcOutcome:
    timed_out: bool
    returncode: int | None
    stdout: str
    stderr: str


def _run_isolated(runner_code: str, timeout_seconds: float, memory_limit_mb: int) -> _ProcOutcome:
    env = _minimal_env()
    job_ctx: Any = None
    with tempfile.TemporaryDirectory(prefix="shuati_sbx_") as tmpdir:
        popen_kwargs: dict[str, Any] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "cwd": tmpdir,
            "env": env,
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        proc = subprocess.Popen([sys.executable, "-c", runner_code], **popen_kwargs)

        if sys.platform == "win32":
            job_ctx = _create_windows_job_object(memory_limit_mb)
            if job_ctx is not None:
                kernel32, h_job = job_ctx
                try:
                    proc_handle = int(getattr(proc, "_handle", 0))
                    if proc_handle:
                        kernel32.AssignProcessToJobObject(h_job, proc_handle)
                except Exception:
                    pass

        try:
            stdout, stderr = proc.communicate(timeout=timeout_seconds)
            return _ProcOutcome(False, proc.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            try:
                stdout, stderr = proc.communicate(timeout=3.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
            return _ProcOutcome(True, proc.returncode, stdout, stderr)
        finally:
            if job_ctx is not None:
                kernel32, h_job = job_ctx
                try:
                    kernel32.CloseHandle(h_job)
                except Exception:
                    pass


def _find_sentinel(stdout: str, sentinel: str) -> str | None:
    payload: str | None = None
    prefix = sentinel + " "
    for line in stdout.splitlines():
        if line.startswith(prefix):
            payload = line[len(prefix) :].strip()
    return payload


def run_verify_script(
    code_str: str,
    *,
    timeout_seconds: float = 5.0,
    memory_limit_mb: int = 512,
) -> SympySandboxResult:
    """跨平台隔离子进程执行 verify() -> dict[str, str | list[str]]。"""
    probe = validate_script_ast(code_str)
    if not probe.ok:
        return SympySandboxResult(
            ok=False,
            status="AST_REJECTED",
            values=None,
            stderr_tail=f"AST probe rejected: {probe.reason}",
        )

    runner_code = (
        _build_rlimit_preamble(memory_limit_mb, timeout_seconds)
        + _VERIFY_PREAMBLE
        + "_USER_CODE = "
        + json.dumps(code_str)
        + "\n"
        + _VERIFY_MAIN
    )
    outcome = _run_isolated(runner_code, timeout_seconds, memory_limit_mb)
    tail = (outcome.stderr or "")[-_STDERR_TAIL_CHARS:]

    if outcome.timed_out:
        return SympySandboxResult(ok=False, status="TIMEOUT", values=None, stderr_tail=tail)

    result_line = _find_sentinel(outcome.stdout, _RESULT_SENTINEL)
    if result_line is not None:
        try:
            parsed = json.loads(result_line)
            if isinstance(parsed, dict) and parsed:
                return SympySandboxResult(ok=True, status="PASSED", values=parsed, stderr_tail=tail)
        except ValueError:
            pass
        return SympySandboxResult(
            ok=False,
            status="FAILED",
            values=None,
            stderr_tail=tail or "malformed result sentinel",
        )

    error_line = _find_sentinel(outcome.stdout, _ERROR_SENTINEL)
    if error_line is not None:
        detail = error_line
        try:
            detail = str(json.loads(error_line).get("detail", error_line))
        except ValueError:
            pass
        return SympySandboxResult(
            ok=False, status="FAILED", values=None, stderr_tail=detail[-_STDERR_TAIL_CHARS:]
        )

    if sys.platform != "win32":
        sigxcpu = getattr(signal, "SIGXCPU", None)
        sigkill = getattr(signal, "SIGKILL", None)
        if outcome.returncode in {s for s in (-sigxcpu if sigxcpu else None, -sigkill if sigkill else None) if s is not None}:
            return SympySandboxResult(ok=False, status="TIMEOUT", values=None, stderr_tail=tail)

    return SympySandboxResult(
        ok=False,
        status="FAILED",
        values=None,
        stderr_tail=tail or f"sandbox exited with code {outcome.returncode}",
    )


# ---------------------------------------------------------------------------
# 3. 隔离子进程数学等价判定（绝不在宿主进程调用 parse_expr）
# ---------------------------------------------------------------------------

_MAX_EXPR_CHARS = 4000
_FORBIDDEN_EXPR_CHARS = frozenset("'\"\\#$@&`~")


def _is_safe_expr_input(text: str) -> bool:
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped or len(stripped) > _MAX_EXPR_CHARS:
        return False
    if "__" in stripped:
        return False
    if any(ch in _FORBIDDEN_EXPR_CHARS for ch in stripped):
        return False
    # 拦截除数字小数点及 S.xxx 以外的属性访问
    for idx, ch in enumerate(stripped):
        if ch == ".":
            prev_ch = stripped[idx - 1] if idx > 0 else ""
            next_ch = stripped[idx + 1] if idx + 1 < len(stripped) else ""
            if next_ch.isdigit() or prev_ch.isdigit():
                continue
            if prev_ch == "S" and (idx < 2 or not stripped[idx - 2].isalnum()):
                continue
            return False
    return all(ch.isprintable() or ch.isspace() for ch in stripped)


_SYMBOLIC_CHECK_RUNTIME = '''
import json as _json
import re as _re
import traceback as _traceback

from sympy import (
    Abs,
    Add,
    EmptySet,
    FiniteSet,
    Float,
    Integer,
    Interval,
    Mul,
    Pow,
    Rational,
    S,
    Symbol,
    Union,
    acos,
    asin,
    atan,
    binomial,
    cos,
    exp,
    factorial,
    gcd,
    lcm,
    log,
    nsimplify,
    oo,
    pi,
    reduce_inequalities,
    simplify,
    sin,
    solve,
    solveset,
    sqrt,
    tan,
)
from sympy.parsing.sympy_parser import (
    convert_xor as _convert_xor,
    implicit_multiplication_application as _implicit_mul,
    parse_expr as _parse_expr,
    standard_transformations as _std_transforms,
)

_TRANSFORMS = _std_transforms + (_implicit_mul, _convert_xor)
_SAFE_GLOBALS = {
    "__builtins__": {},
    "Abs": Abs,
    "Add": Add,
    "EmptySet": EmptySet,
    "FiniteSet": FiniteSet,
    "Float": Float,
    "Integer": Integer,
    "Interval": Interval,
    "Mul": Mul,
    "Pow": Pow,
    "Rational": Rational,
    "S": S,
    "Symbol": Symbol,
    "Union": Union,
    "acos": acos,
    "asin": asin,
    "atan": atan,
    "binomial": binomial,
    "cos": cos,
    "E": S.Exp1,
    "exp": exp,
    "factorial": factorial,
    "gcd": gcd,
    "I": S.ImaginaryUnit,
    "inf": oo,
    "lcm": lcm,
    "ln": log,
    "log": log,
    "oo": oo,
    "pi": pi,
    "sin": sin,
    "sqrt": sqrt,
    "tan": tan,
}


def safe_parse(text):
    s = (text or "").strip()
    if not s:
        return None
    if "__" in s or "'" in s or '"' in s or "\\\\" in s:
        return None
    s = s.replace("∞", "oo").replace("+oo", "oo").replace("+inf", "oo")
    try:
        expr = _parse_expr(s, local_dict={}, global_dict=_SAFE_GLOBALS, transformations=_TRANSFORMS)
        if isinstance(expr, (Interval, FiniteSet, Union, type(EmptySet))):
            return expr
        return nsimplify(expr, rational=True)
    except Exception:
        return None


def _equal_expr_values(e1, e2, allow_pi_314):
    if e1 is None or e2 is None:
        return False
    try:
        if e1 == e2:
            return True
        if hasattr(e1, "canonical") and hasattr(e2, "canonical"):
            if e1.canonical == e2.canonical:
                return True
            return False
        if simplify(e1 - e2) == 0:
            return True
        if allow_pi_314:
            p1_has = e1.has(pi)
            p2_has = e2.has(pi)
            if p1_has != p2_has:
                e1_sub = e1.subs(pi, Rational(314, 100))
                e2_sub = e2.subs(pi, Rational(314, 100))
                if not e1_sub.free_symbols and not e2_sub.free_symbols:
                    if simplify(e1_sub - e2_sub) == 0:
                        return True
        return False
    except Exception:
        return False


def _check_expression(a, b, allow_pi_314):
    if "=" in a and "=" in b:
        pa = a.split("=")
        pb = b.split("=")
        if len(pa) == 2 and len(pb) == 2:
            ea = safe_parse(pa[0])
            ea_r = safe_parse(pa[1])
            eb = safe_parse(pb[0])
            eb_r = safe_parse(pb[1])
            if ea is None or ea_r is None or eb is None or eb_r is None:
                return False
            d1 = ea - ea_r
            d2 = eb - eb_r
            if simplify(d1 - d2) == 0:
                return True
            if not (d1.free_symbols or d2.free_symbols):
                return False
            if simplify(d1 + d2) == 0:
                return True
            if d2 != 0:
                ratio = simplify(d1 / d2)
                if ratio.is_constant() and ratio != 0:
                    return True
            return False
        return False

    if "=" in a and "=" not in b:
        pa = a.split("=")
        if len(pa) == 2 and safe_parse(pa[0]) is not None and safe_parse(pa[0]).is_Symbol:
            return _equal_expr_values(safe_parse(pa[1]), safe_parse(b), allow_pi_314)
        return False

    if "=" in b and "=" not in a:
        pb = b.split("=")
        if len(pb) == 2 and safe_parse(pb[0]) is not None and safe_parse(pb[0]).is_Symbol:
            return _equal_expr_values(safe_parse(a), safe_parse(pb[1]), allow_pi_314)
        return False

    return _equal_expr_values(safe_parse(a), safe_parse(b), allow_pi_314)


def _parse_ratio_terms(text):
    s = text.replace("：", ":").strip()
    if ":" in s:
        parts = [p.strip() for p in s.split(":")]
        if len(parts) < 2 or any(not p for p in parts):
            return None
        parsed = [safe_parse(p) for p in parts]
        if any(x is None for x in parsed):
            return None
        return parsed
    val = safe_parse(s)
    if val is None:
        return None
    return [val]


def _check_ratio(a, b, allow_pi_314):
    ta = _parse_ratio_terms(a)
    tb = _parse_ratio_terms(b)
    if ta is None or tb is None:
        return False
    # 二项比 vs 分数/小数互认
    if len(ta) == 2 and len(tb) == 1:
        if simplify(ta[1]) == 0:
            return False
        return _equal_expr_values(simplify(ta[0] / ta[1]), tb[0], allow_pi_314)
    if len(tb) == 2 and len(ta) == 1:
        if simplify(tb[1]) == 0:
            return False
        return _equal_expr_values(ta[0], simplify(tb[0] / tb[1]), allow_pi_314)
    if len(ta) != len(tb):
        return False
    if len(ta) == 1:
        return _equal_expr_values(ta[0], tb[0], allow_pi_314)

    # 多项连比：寻找共同非零比例系数 c，使得 ta[i] == c * tb[i] 对所有 i 成立
    if allow_pi_314 and (any(x.has(pi) for x in ta) != any(x.has(pi) for x in tb)):
        ta = [x.subs(pi, Rational(314, 100)) for x in ta]
        tb = [x.subs(pi, Rational(314, 100)) for x in tb]

    pivot = None
    for i, (xa, xb) in enumerate(zip(ta, tb)):
        za = simplify(xa) == 0
        zb = simplify(xb) == 0
        if za != zb:
            return False
        if not za and pivot is None:
            pivot = i
    if pivot is None:
        return False

    c = simplify(ta[pivot] / tb[pivot])
    if simplify(c) == 0:
        return False
    if c.is_real is True and c <= 0:
        return False
    for xa, xb in zip(ta, tb):
        if simplify(xa - c * xb) != 0:
            return False
    return True


_INTERVAL_RE = _re.compile(r"^([\\[\\(])\\s*([^\\[\\]\\(\\)]+?)\\s*,\\s*([^\\[\\]\\(\\)]+?)\\s*([\\]\\)])$")


def _split_top_level(text, sep_char=","):
    parts = []
    depth = 0
    buf = []
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if ch == sep_char and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf).strip())
    return [p for p in parts if p]


def _parse_single_set_token(tok):
    t = tok.strip()
    if not t:
        return None
    if t in ("∅", "emptyset", "EmptySet", "无解", "{}"):
        return EmptySet
    if t in ("R", "ℝ", "(-oo,oo)", "(-inf,inf)", "(-∞,+∞)"):
        return S.Reals

    # SymPy Set expression directly
    if t.startswith(("Interval", "FiniteSet", "Union", "EmptySet")):
        val = safe_parse(t)
        if isinstance(val, (Interval, FiniteSet, Union, type(EmptySet))):
            return val

    # {a, b, c}
    if t.startswith("{") and t.endswith("}"):
        inner = t[1:-1].strip()
        if not inner:
            return EmptySet
        items = [safe_parse(p) for p in _split_top_level(inner, ",")]
        if any(x is None for x in items):
            return None
        return FiniteSet(*items)

    # [a, b] / (a, b] ...
    m = _INTERVAL_RE.match(t)
    if m:
        lbr, left_s, right_s, rbr = m.groups()
        left_v = safe_parse(left_s)
        right_v = safe_parse(right_s)
        if left_v is None or right_v is None:
            return None
        return Interval(left_v, right_v, left_open=(lbr == "("), right_open=(rbr == ")"))

    # 不等式链: 1 < x <= 3 或 x >= 2
    if any(op in t for op in ("<=", ">=", "<", ">")):
        parts = _re.split(r"(<=|>=|<|>)", t)
        if len(parts) >= 3 and len(parts) % 2 == 1:
            conds = []
            syms = set()
            for idx in range(0, len(parts) - 2, 2):
                lhs = safe_parse(parts[idx])
                op = parts[idx + 1]
                rhs = safe_parse(parts[idx + 2])
                if lhs is None or rhs is None:
                    return None
                syms |= lhs.free_symbols | rhs.free_symbols
                if op == "<":
                    conds.append(lhs < rhs)
                elif op == "<=":
                    conds.append(lhs <= rhs)
                elif op == ">":
                    conds.append(lhs > rhs)
                elif op == ">=":
                    conds.append(lhs >= rhs)
            if len(syms) == 1:
                var = next(iter(syms))
                return reduce_inequalities(conds, var).as_set()

    # 单方程 x^2 - 1 = 0 或 x = 2
    if "=" in t and t.count("=") == 1:
        lhs_s, rhs_s = t.split("=")
        lhs = safe_parse(lhs_s)
        rhs = safe_parse(rhs_s)
        if lhs is None or rhs is None:
            return None
        diff = lhs - rhs
        syms = diff.free_symbols
        if len(syms) == 1:
            var = next(iter(syms))
            return solveset(diff, var, S.Reals)
        return None

    # 逗号分隔有限集: "1, 2"
    if "," in t:
        items = [safe_parse(p) for p in _split_top_level(t, ",")]
        if items and all(x is not None for x in items):
            return FiniteSet(*items)

    # 单个标量元素视为单元素有限集
    val = safe_parse(t)
    if val is not None:
        return FiniteSet(val)
    return None


def _parse_set_or_interval(text):
    s = (text or "").strip()
    if not s:
        return None
    # 剥离常见引导前缀 "x ∈ " / "x in "
    s = _re.sub(r"^[A-Za-z]\\s*(?:∈|in)\\s*", "", s).strip()
    # 按并集符号拆分
    union_parts = _re.split(r"\\s*(?:∪|\\bU\\b|\\bor\\b|或)\\s*", s)
    sets = []
    for part in union_parts:
        part = part.strip()
        if not part:
            continue
        parsed = _parse_single_set_token(part)
        if parsed is None:
            return None
        sets.append(parsed)
    if not sets:
        return None
    res = sets[0]
    for nxt in sets[1:]:
        res = Union(res, nxt)
    return res


def _solutions_equivalent(eqs_a, eqs_b):
    try:
        pa = []
        pb = []
        for t in eqs_a:
            if "=" not in t or t.count("=") != 1:
                return False
            l, r = t.split("=")
            lv, rv = safe_parse(l), safe_parse(r)
            if lv is None or rv is None:
                return False
            pa.append(lv - rv)
        for t in eqs_b:
            if "=" not in t or t.count("=") != 1:
                return False
            l, r = t.split("=")
            lv, rv = safe_parse(l), safe_parse(r)
            if lv is None or rv is None:
                return False
            pb.append(lv - rv)
        ua = set().union(*(e.free_symbols for e in pa))
        ub = set().union(*(e.free_symbols for e in pb))
        if not ua or not ub:
            return False
        union_syms = ua | ub
        if len(pa) > 8 or len(pb) > 8 or len(union_syms) > 8:
            return False
        U = sorted(union_syms, key=lambda sym: sym.name)
        sols_a = solve(pa, U, dict=True)
        sols_b = solve(pb, U, dict=True)
        if not sols_a or not sols_b:
            return False
        for sol in sols_a:
            for e in pb:
                if simplify(e.subs(sol)) != 0:
                    return False
        for sol in sols_b:
            for e in pa:
                if simplify(e.subs(sol)) != 0:
                    return False
        return True
    except Exception:
        return False


def _check_set_or_interval(a, b):
    if ";" in a or "\\n" in a or ";" in b or "\\n" in b:
        eqs_a = [x.strip() for x in _re.split(r"[;\\n]+", a) if x.strip()]
        eqs_b = [x.strip() for x in _re.split(r"[;\\n]+", b) if x.strip()]
        if eqs_a and eqs_b and all("=" in x for x in eqs_a + eqs_b):
            if _solutions_equivalent(eqs_a, eqs_b):
                return True
    sa = _parse_set_or_interval(a)
    sb = _parse_set_or_interval(b)
    if sa is None or sb is None:
        return False
    try:
        if sa == sb:
            return True
        return simplify(sa.symmetric_difference(sb)) == EmptySet
    except Exception:
        return False


_equal = False
try:
    _mode = _PAYLOAD["mode"]
    _a = _PAYLOAD["a"]
    _b = _PAYLOAD["b"]
    _pi_314 = bool(_PAYLOAD["allow_pi_314"])
    if _mode == "RATIO":
        _equal = _check_ratio(_a, _b, _pi_314)
    elif _mode == "SET_OR_INTERVAL":
        _equal = _check_set_or_interval(_a, _b)
    else:
        _equal = _check_expression(_a, _b, _pi_314)
except BaseException:
    _traceback.print_exc()
    _equal = False
print("###RESULT### " + _json.dumps({"equal": bool(_equal)}))
'''


def run_symbolic_check_in_sandbox(
    expr_a: str,
    expr_b: str,
    *,
    mode: Literal["EXPRESSION", "RATIO", "SET_OR_INTERVAL"] = "EXPRESSION",
    allow_pi_314: bool = False,
    timeout_seconds: float = 5.0,
    memory_limit_mb: int = 512,
) -> tuple[bool, str | None]:
    """在隔离子进程中通过受控 safe_parse() 判断两个表达式/比值/解集是否数学等价。
    返回 (is_equal, error_kind)。绝不在宿主进程调用 parse_expr！
    """
    if not isinstance(expr_a, str) or not isinstance(expr_b, str):
        return False, "BAD_INPUT"
    a_clean = expr_a.strip()
    b_clean = expr_b.strip()
    if not a_clean or not b_clean:
        return False, "BAD_INPUT"
    if not _is_safe_expr_input(a_clean) or not _is_safe_expr_input(b_clean):
        return False, "UNSAFE_EXPRESSION"

    payload = json.dumps(
        {
            "a": a_clean,
            "b": b_clean,
            "mode": mode,
            "allow_pi_314": allow_pi_314,
        },
        ensure_ascii=False,
    )
    runner_code = (
        _build_rlimit_preamble(memory_limit_mb, timeout_seconds)
        + "import json as _json\n"
        + "_PAYLOAD = _json.loads("
        + json.dumps(payload)
        + ")\n"
        + _SYMBOLIC_CHECK_RUNTIME
    )
    outcome = _run_isolated(runner_code, timeout_seconds, memory_limit_mb)
    if outcome.timed_out:
        return False, "TIMEOUT"
    if outcome.returncode != 0:
        return False, "RUNTIME_ERROR"
    line = _find_sentinel(outcome.stdout, _RESULT_SENTINEL)
    if line is None:
        return False, "BAD_OUTPUT"
    try:
        return bool(json.loads(line)["equal"]), None
    except (ValueError, KeyError, TypeError):
        return False, "BAD_OUTPUT"
