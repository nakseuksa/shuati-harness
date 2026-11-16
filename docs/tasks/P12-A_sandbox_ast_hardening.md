# Task Contract: P12-A — SymPy Sandbox AST Attribute & Name Hardening (Risk 3)

## Context & Goal
`backend/sandbox/sympy_verifier.py` currently blocks imports outside `_ALLOWED_MODULES`, dunder attributes (`attr.startswith("__")`), and direct `ast.Call` on `_FORBIDDEN_CALL_NAMES` (`open`, `eval`, `exec`, `__import__`, `getattr`, `globals`, `locals`).
However, an untrusted SymPy script can still escape through whitelisted modules or aliases:
- Attribute chains such as `import sympy; sympy.sys.modules['os'].system('id')` or `fractions.os` or internal `_`-prefixed attributes (`sympy._...`) bypass `startswith("__")` and `_FORBIDDEN_CALL_NAMES`.
- Reference aliasing such as `f = eval; f("...")` or referencing `sys`, `os`, `builtins`, `importlib`, `subprocess`, `compile`, `setattr`, `delattr`, `vars` outside direct `Call(Name)` is not rejected by `validate_script_ast`.

This task hardens `validate_script_ast` (and runtime defense if safe for legitimate SymPy usage) without breaking any existing valid SymPy / `Fraction` / `Decimal` / `math` / `itertools` solver or equivalence checks.

## File Whitelist (Strict)
- `backend/sandbox/sympy_verifier.py`
- `tests/test_sympy_sandbox.py`

## Requirements
1. Expand static AST validation in `backend/sandbox/sympy_verifier.py`:
   - Keep `attr.startswith("__")` rejected with reason code `"DUNDER_ATTR"` so existing tests remain intact.
   - Reject any single-leading-underscore private attribute (`attr.startswith("_")`) AND any security-sensitive attribute name in a dedicated `_FORBIDDEN_ATTR_NAMES` set (`"sys"`, `"os"`, `"subprocess"`, `"importlib"`, `"builtins"`, `"ctypes"`, `"socket"`, `"pathlib"`, `"shutil"`, `"io"`, `"modules"`, `"system"`, `"popen"`, `"spawn"`, `"fork"`, `"execv"`, `"execl"`, `"execve"`, `"environ"`, `"read_text"`, `"write_text"`, `"read_bytes"`, `"write_bytes"`, `"open"`, `"eval"`, `"exec"`, `"compile"`, `"getattr"`, `"setattr"`, `"delattr"`, `"globals"`, `"locals"`, `"vars"`, `"breakpoint"`) with reason code `"FORBIDDEN_ATTR"`.
   - Expand `_FORBIDDEN_CALL_NAMES` and also check `ast.Name` references (in addition to `ast.Call` on `ast.Name` or `ast.Attribute`):
     - Direct call `func.id in _FORBIDDEN_CALL_NAMES` still returns `"FORBIDDEN_CALL"` (preserving existing error code tests).
     - Referencing sensitive names via `ast.Name` (e.g., `f = eval`, `open`, `exec`, `compile`, `getattr`, `setattr`, `delattr`, `globals`, `locals`, `vars`, `breakpoint`, `input`, `memoryview`, `__import__`, `sys`, `os`, `subprocess`, `importlib`, `builtins`, `ctypes`, `socket`, `pathlib`, `shutil`, `pickle`, `marshal`) outside safe parameter shadowing? Reject with `"FORBIDDEN_CALL"` / `"FORBIDDEN_NAME"` (prefer `"FORBIDDEN_CALL"` if in `_FORBIDDEN_CALL_NAMES`, `"FORBIDDEN_NAME"` otherwise) — ensure no legitimate SymPy math variable names (like `x`, `y`, `t`, `m`, `g`, `v`, `F`, `E`, `N`, `ans`) are blocked!
   - Also reject `ast.Call` where `func` is `ast.Attribute` and `func.attr in _FORBIDDEN_CALL_NAMES | _FORBIDDEN_ATTR_NAMES`.
2. Verify `_SOLVER_PREAMBLE` compatibility: any added runtime hardening must NOT break legitimate SymPy expressions (`sympy.solve`, `sympy.Eq`, `sympy.Rational`, `sympy.sympify`, `fractions.Fraction`, `decimal.Decimal`, `math`, `itertools`). If runtime import restriction is added in `_SOLVER_PREAMBLE`, keep `_ALLOWED_MODULES` submodules working (`name.split(".")[0] in _ALLOWED_MODULES`). (Keep standard AST checks clean and verified via subprocess tests.)
3. Unit tests in `tests/test_sympy_sandbox.py`:
   - Reject `import sympy; sympy.sys.modules['os'].system('echo pwned')`
   - Reject `import fractions; fractions.sys` / `sympy.os`
   - Reject `import sympy; sympy._private` (single underscore internal attr)
   - Reject aliased forbidden calls (`f = eval; f("1+1")`, `g = getattr`)
   - Reject forbidden method calls (`x.read_text()`, `m.popen("id")`)
   - Confirm legitimate SymPy scripts (`Eq`, `solve`, `Fraction`, `math.sqrt`, `Decimal`, `itertools.combinations`) still pass AST validation and execute cleanly in the sandbox.

## Constraints
- Do NOT edit files outside the whitelist.
- Do NOT run `git commit` or `git push`.
- Targeted verification after edits:
  `.venv/bin/python -m pytest tests/test_sympy_sandbox.py -q && .venv/bin/ruff check backend/sandbox/sympy_verifier.py tests/test_sympy_sandbox.py && .venv/bin/mypy backend/sandbox/sympy_verifier.py`
