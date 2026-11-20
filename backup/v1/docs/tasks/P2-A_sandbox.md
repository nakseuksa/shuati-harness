# P2-A 任务契约：AST 探针 + SymPy 子进程沙盒 + 代数等价引擎

## 上下文
项目：数理自适应刷题 Harness。你负责 Phase 2 的受限校验沙盒——衍生题数学校验的确定性主防线。
必读规格：`docs/designs/adaptive_practice_harness.md` 的 **§3.2 第 2 条（内核先行 + AST 静态探针 + 平台兼容沙盒）**、**§3.3 第 1 条（代数等价判据）**、Target File **#7**、**#16**、Non-Functional Requirements 的 Security 段、Risks 表 macOS 行。设计文档是唯一权威规格。

## 你拥有的文件
- `backend/sandbox/sympy_verifier.py`
- `tests/test_sympy_sandbox.py`

**禁止**触碰其他文件。**禁止** git commit/push。

## 环境
- 项目根：`/Users/wanghaichen/0_work/刷题harness`；一律用 `.venv/bin/` 可执行文件
- 门禁：`.venv/bin/python -m pytest tests/test_sympy_sandbox.py -v` + ruff/mypy 对你的文件零告警
- 本机为 macOS：RLIMIT_AS 降级路径必须实测通过

## API 契约（下游 scaffold_engine 将按此调用）
```python
@dataclass
class ASTValidationResult:
    ok: bool
    reason: str  # 'OK' | 'VACUOUS_SCRIPT_REJECTED' | 'FORBIDDEN_IMPORT' | 'FORBIDDEN_CALL' | 'FORBIDDEN_ATTR' | 'SYNTAX_ERROR'

def validate_script_ast(code_str: str) -> ASTValidationResult: ...

@dataclass
class SandboxExecutionResult:
    ok: bool
    params: dict[str, str | int | float] | None
    answer_latex: str | None
    error_kind: str | None   # 'TIMEOUT' | 'RUNTIME_ERROR' | 'BAD_CONTRACT' | 'AST_REJECTED' | None
    stderr_tail: str         # 末尾 ~500 字符，供重试反馈

def run_sympy_in_sandbox(
    code_str: str,
    timeout_s: float | None = None,   # 缺省 settings.SYMPY_TIMEOUT_SECONDS
    max_mem_mb: int | None = None,    # 缺省 settings.SYMPY_MAX_MEMORY_MB
) -> SandboxExecutionResult: ...

def verify_equation_equivalence(user_expr_str: str, standard_expr_str: str) -> bool: ...
```

## 实现规格
### 1. AST 静态探针
- 导入白名单：仅 `sympy`、`math`、`fractions`（含 `from sympy import ...` 形式），其余一律 `FORBIDDEN_IMPORT`
- 禁用调用：`open/eval/exec/__import__/getattr/globals/locals`（按 Name 与 Attribute 末段匹配）→ `FORBIDDEN_CALL`
- 禁止任何双下划线属性访问（`attr.startswith("__")`，如 `__subclasses__`/`__class__`）→ `FORBIDDEN_ATTR`
- 必含至少一个真实符号求解/化简调用：`solve/solveset/dsolve/simplify/Eq/integrate/limit/Matrix`（Name 或 Attribute 末段），否则 `VACUOUS_SCRIPT_REJECTED`
- 语法错误 → `SYNTAX_ERROR`

### 2. 子进程沙盒
- 先过 AST 探针，拒绝则直接返回 `AST_REJECTED`，不起进程
- 子进程入口：`subprocess.run([sys.executable, "-c", runner_code], capture_output=True, text=True, timeout=timeout_s, start_new_session=True)`；超时须杀整个进程组（`os.killpg`）后返回 `TIMEOUT`
- runner 内按平台设置 rlimit：Linux `RLIMIT_AS=max_mem_mb` + `RLIMIT_CPU=ceil(timeout)`；macOS 仅 `RLIMIT_CPU`，`RLIMIT_AS` 捕获 `(ValueError, OSError)` 降级忽略
- 执行用户代码后调用 `solve_kernel()`，校验返回为 dict 且含 `params`(dict) 与 `answer_latex`(非空 str)，否则 `BAD_CONTRACT`；结果经 stdout 以 JSON 哨兵行传回（如 `###RESULT### {json}`）
- 运行期异常 → `RUNTIME_ERROR` + stderr_tail

### 3. 代数等价引擎
- `parse_expr(..., transformations=standard_transformations + (implicit_multiplication_application, convert_xor))`，再 `nsimplify(..., rational=True)`
- 含 `=` 则移项为 `lhs - rhs`；多方程按 `;` 或换行切分
- 判据（任一成立即等价）：`simplify(e1-e2)==0`；`simplify(e1+e2)==0`（相反数）；`simplify(e1/e2).is_constant()` 且非零（常数比率）
- 解析失败/超时保护返回 False，不抛异常到调用方；单方程判定加 sympy 超时保护（可用信号或子进程，自行选择并测试）

## 测试要求（设计 #16 逐条 + 补充）
- 恒真伪脚本（直接 return 常量、无符号调用）→ `VACUOUS_SCRIPT_REJECTED`
- `import os` / `__import__("os")` / `().__class__.__subclasses__()` → 对应拦截码
- 合法脚本（如 solve 一元二次）→ ok 且 params/answer_latex 正确
- 死循环脚本 → 5s 内 `TIMEOUT`（测试用 timeout_s=1.0 加速，勿真等 5s）
- macOS 下不因 RLIMIT_AS 崩溃（本机实测）
- BAD_CONTRACT：solve_kernel 返回缺字段/空 answer
- 等价引擎：`m*v**2/2 - q*U` vs `q*U - 1/2*m*v**2`（相反数）；`2*x=4` vs `x=2`（移项+常数倍）；隐式乘法 `2x` vs `2*x`；`x^2` vs `x**2`；不等价式返回 False；垃圾输入返回 False 不抛异常

## 完成标准
门禁全绿。最终消息汇报：文件、测试数、门禁摘要、macOS 实测结论。
