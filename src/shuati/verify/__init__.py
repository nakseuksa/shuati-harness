"""分小问验真、跨平台 SymPy 沙盒与等价判定引擎。"""

from shuati.verify.answer_parts import (
    derive_verification_mode,
    normalize_answer_parts,
    normalize_choice_label,
    normalize_part_label,
    normalize_symbol_fill,
)
from shuati.verify.equivalence import (
    EquivalenceSummary,
    PartVerdict,
    compare_answer_parts,
    latex_to_plain,
    strip_units,
)
from shuati.verify.sympy_sandbox import (
    ASTValidationResult,
    SympySandboxResult,
    run_symbolic_check_in_sandbox,
    run_verify_script,
    validate_script_ast,
)

__all__ = [
    "ASTValidationResult",
    "EquivalenceSummary",
    "PartVerdict",
    "SympySandboxResult",
    "compare_answer_parts",
    "derive_verification_mode",
    "latex_to_plain",
    "normalize_answer_parts",
    "normalize_choice_label",
    "normalize_part_label",
    "normalize_symbol_fill",
    "run_symbolic_check_in_sandbox",
    "run_verify_script",
    "strip_units",
    "validate_script_ast",
]
