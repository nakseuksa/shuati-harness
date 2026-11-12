"""P2-B：VLM 多模态诊断服务测试（全程 USE_MOCK_LLM=True，零真实网络）。

规格：docs/tasks/P2-B_gateway_vlm.md 测试要求 +
docs/designs/adaptive_practice_harness.md §3.1 第 3/4 条、Target File #8。
AST 探针断言依赖并行开发的 backend.sandbox.sympy_verifier（未就位则 importorskip）。
"""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from backend.config import settings
from backend.services import llm_mocks, vlm_diagnosis
from backend.services.llm_gateway import image_path_to_data_url
from backend.services.vlm_diagnosis import (
    TRAP_TAXONOMY,
    DiagnosisPayload,
    analyze_question_and_draft,
)

STEM_MD = (
    "汽车以速度 $v$ 通过半径 $R = 40\\,\\text{m}$ 的拱形桥最高点，"
    "取 $g = 10\\,\\text{m/s}^2$。求汽车不脱离桥面的最大速度 $v_{max}$。"
)
TREE: dict[str, list[str]] = {"力学": ["圆周运动", "机械能"], "电磁学": ["磁场"]}
GRADE = "高中物理-高二"


@pytest.fixture(autouse=True)
def _mock_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "USE_MOCK_LLM", True)


@pytest.fixture()
def images(tmp_path: Path) -> tuple[Path, Path]:
    stem = tmp_path / "stem.webp"
    stem.write_bytes(b"RIFF-fake-stem-webp")
    draft = tmp_path / "draft.webp"
    draft.write_bytes(b"RIFF-fake-draft-webp")
    return stem, draft


@pytest.fixture()
def chat_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """间谍包装 chat_json：记录调用参数后仍走真实 Mock 路由。"""
    calls: list[dict[str, Any]] = []
    real_chat_json = vlm_diagnosis.chat_json

    async def spy(messages: Any, schema: Any, **kwargs: Any) -> Any:
        calls.append({"messages": messages, "schema": schema, **kwargs})
        return await real_chat_json(messages, schema, **kwargs)

    monkeypatch.setattr(vlm_diagnosis, "chat_json", spy)
    return calls


def default_payload() -> dict[str, Any]:
    return copy.deepcopy(llm_mocks.MOCK_RESPONSES["diagnosis"])


def install_payload(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    monkeypatch.setitem(llm_mocks.MOCK_RESPONSES, "diagnosis", payload)


async def run_diagnosis(
    stem_images: list[Path], draft_images: list[Path]
) -> DiagnosisPayload:
    return await analyze_question_and_draft(STEM_MD, stem_images, draft_images, GRADE, TREE)


# ---------------------------------------------------------------------------
# 默认 fixture 全链路
# ---------------------------------------------------------------------------


async def test_default_fixture_full_chain(images: tuple[Path, Path]) -> None:
    stem, draft = images
    payload = await run_diagnosis([stem], [draft])
    trace = payload.trace
    assert [s.step_index for s in trace.steps] == [1, 2, 3]
    assert (trace.c_score, trace.s_score, trace.t_score, trace.l_score) == (3, 3, 2, 3)
    assert trace.trap_tags == ["TRAP_DOMAIN_BOUNDARY"]
    assert trace.standard_answer_latex
    for step in trace.steps:
        assert step.masked_conditions
        assert step.probe_question_md
        assert step.l1_module and step.l2_entity and step.l3_operator

    alignment = payload.alignment
    assert alignment.draft_steps_latex  # 一次性转录非空
    assert alignment.matched_branch_id == "canonical"
    assert alignment.alt_branch_steps is None
    assert alignment.last_valid_step_index == 1
    assert alignment.stalled_step_index == 2  # 草稿对齐卡在 Step 2
    assert alignment.error_category == "TRAP_OR_CALC_ERROR"
    assert alignment.trap_taxonomy_tag == "TRAP_DOMAIN_BOUNDARY"


async def test_single_vlm_call_with_multiple_images(
    images: tuple[Path, Path], chat_calls: list[dict[str, Any]]
) -> None:
    stem, draft = images
    await run_diagnosis([stem, stem], [draft, draft])
    assert len(chat_calls) == 1  # 单次 VLM 调用（NFR：一次性转录）
    call = chat_calls[0]
    assert call["purpose"] == "diagnosis"
    assert call["model"] == settings.VLM_MODEL_NAME
    assert call["schema"] is DiagnosisPayload
    parts = call["messages"][1]["content"]
    image_parts = [p for p in parts if p["type"] == "image_url"]
    assert len(image_parts) == 4
    assert image_parts[0]["image_url"]["url"] == image_path_to_data_url(stem)
    assert image_parts[2]["image_url"]["url"] == image_path_to_data_url(draft)
    assert all(p["image_url"]["url"].startswith("data:image/webp;base64,") for p in image_parts)


async def test_prompt_injects_tree_traps_and_anchors(
    images: tuple[Path, Path], chat_calls: list[dict[str, Any]]
) -> None:
    stem, draft = images
    await run_diagnosis([stem], [draft])
    messages = chat_calls[0]["messages"]
    system_prompt = messages[0]["content"]
    for tag in TRAP_TAXONOMY:  # 六大算子枚举全部注入
        assert tag in system_prompt
    for anchor in ("C∈[1,4]", "S∈[1,3]", "T∈[1,3]", "L∈[1,3]"):  # 四维行为级锚点
        assert anchor in system_prompt
    assert "JSON Schema" in system_prompt
    user_text = messages[1]["content"][0]["text"]
    assert GRADE in user_text
    assert STEM_MD in user_text
    for l1, l2_list in TREE.items():  # 既有 L1/L2 树骨架注入
        assert l1 in user_text
        for l2 in l2_list:
            assert l2 in user_text


# ---------------------------------------------------------------------------
# 空白草稿短路
# ---------------------------------------------------------------------------


async def test_blank_draft_short_circuit(images: tuple[Path, Path]) -> None:
    stem, _ = images
    payload = await run_diagnosis([stem], [])
    alignment = payload.alignment
    assert alignment.draft_steps_latex == []
    assert alignment.matched_branch_id == "canonical"
    assert alignment.alt_branch_steps is None
    assert alignment.last_valid_step_index == 0
    assert alignment.stalled_step_index == 1
    assert alignment.error_category == "CONCEPT_BOTTLENECK"
    assert alignment.trap_taxonomy_tag is None
    assert len(payload.trace.steps) == 3  # trace 仍取 VLM 结果


async def test_blank_draft_sends_no_draft_images(
    images: tuple[Path, Path], chat_calls: list[dict[str, Any]]
) -> None:
    stem, draft = images
    await run_diagnosis([stem], [])
    assert len(chat_calls) == 1
    parts = chat_calls[0]["messages"][1]["content"]
    urls = [p["image_url"]["url"] for p in parts if p["type"] == "image_url"]
    assert urls == [image_path_to_data_url(stem)]  # 仅题干图，无草稿图
    assert image_path_to_data_url(draft) not in urls
    assert any("空白" in p.get("text", "") for p in parts if p["type"] == "text")


async def test_blank_draft_overrides_vlm_alignment(
    images: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """即使 VLM 对齐输出异常，空白草稿也不依赖其结果。"""
    payload = default_payload()
    payload["alignment"]["error_category"] = "ALREADY_MASTERED"
    payload["alignment"]["stalled_step_index"] = 99
    install_payload(monkeypatch, payload)
    stem, _ = images
    result = await run_diagnosis([stem], [])
    assert result.alignment.error_category == "CONCEPT_BOTTLENECK"
    assert result.alignment.stalled_step_index == 1


# ---------------------------------------------------------------------------
# 后处理：stalled 越界修正 / last_valid 约束
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stalled", "last_valid", "expected_stalled", "expected_last_valid"),
    [
        (99, 5, 3, 2),  # 上界越界 → 钳到 len(steps)
        (-2, 0, 1, 0),  # 下界越界 → 钳到 1
        (2, 3, 2, 1),  # last_valid ≥ stalled → 压到 stalled-1
        (0, -1, 1, 0),  # 双越界
    ],
)
async def test_stalled_and_last_valid_corrected(
    images: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    stalled: int,
    last_valid: int,
    expected_stalled: int,
    expected_last_valid: int,
) -> None:
    payload = default_payload()
    payload["alignment"]["stalled_step_index"] = stalled
    payload["alignment"]["last_valid_step_index"] = last_valid
    install_payload(monkeypatch, payload)
    stem, draft = images
    result = await run_diagnosis([stem], [draft])
    assert result.alignment.stalled_step_index == expected_stalled
    assert result.alignment.last_valid_step_index == expected_last_valid
    assert 1 <= result.alignment.stalled_step_index <= len(result.trace.steps)
    assert result.alignment.last_valid_step_index < result.alignment.stalled_step_index


# ---------------------------------------------------------------------------
# 一题多解：alt 分支缺失降级 / 合法保留
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("alt_steps", [None, []])
async def test_alt_branch_missing_downgraded(
    images: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, alt_steps: Any
) -> None:
    payload = default_payload()
    payload["alignment"]["matched_branch_id"] = "user_alt_1"
    payload["alignment"]["alt_branch_steps"] = alt_steps
    install_payload(monkeypatch, payload)
    stem, draft = images
    result = await run_diagnosis([stem], [draft])
    assert result.alignment.matched_branch_id == "canonical"
    assert result.alignment.alt_branch_steps is None


async def test_alt_branch_valid_preserved(
    images: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = default_payload()
    alt_step = copy.deepcopy(payload["trace"]["steps"][0])
    alt_step["step_goal_desc"] = "用户替代解法：以能量视角重新建模"
    payload["alignment"]["matched_branch_id"] = "user_alt_1"
    payload["alignment"]["alt_branch_steps"] = [alt_step]
    install_payload(monkeypatch, payload)
    stem, draft = images
    result = await run_diagnosis([stem], [draft])
    assert result.alignment.matched_branch_id == "user_alt_1"
    assert result.alignment.alt_branch_steps is not None
    assert len(result.alignment.alt_branch_steps) == 1
    assert result.alignment.alt_branch_steps[0].step_goal_desc.startswith("用户替代解法")


# ---------------------------------------------------------------------------
# error_category 与 trap_taxonomy_tag 一致性
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("category", ["CONCEPT_BOTTLENECK", "ALREADY_MASTERED"])
async def test_non_trap_category_clears_tag(
    images: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, category: str
) -> None:
    payload = default_payload()
    payload["alignment"]["error_category"] = category
    payload["alignment"]["trap_taxonomy_tag"] = "TRAP_SIGN_DIRECTION"
    install_payload(monkeypatch, payload)
    stem, draft = images
    result = await run_diagnosis([stem], [draft])
    assert result.alignment.error_category == category
    assert result.alignment.trap_taxonomy_tag is None


async def test_trap_category_missing_tag_raises(
    images: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = default_payload()
    payload["alignment"]["trap_taxonomy_tag"] = None  # TRAP_OR_CALC_ERROR 缺标签
    install_payload(monkeypatch, payload)
    stem, draft = images
    with pytest.raises(ValueError, match="trap_taxonomy_tag 必填"):
        await run_diagnosis([stem], [draft])


# ---------------------------------------------------------------------------
# 四维范围断言与空骨架
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [("c_score", 5), ("s_score", 4), ("t_score", 0), ("l_score", 4)],
)
async def test_trace_dimension_range_violation_raises(
    images: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, field: str, value: int
) -> None:
    payload = default_payload()
    payload["trace"][field] = value
    install_payload(monkeypatch, payload)
    stem, draft = images
    with pytest.raises(ValueError, match="越界"):
        await run_diagnosis([stem], [draft])


async def test_substep_dimension_range_violation_raises(
    images: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = default_payload()
    payload["trace"]["steps"][1]["l_sub"] = 0
    install_payload(monkeypatch, payload)
    stem, draft = images
    with pytest.raises(ValueError, match="越界"):
        await run_diagnosis([stem], [draft])


async def test_empty_steps_raises(
    images: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = default_payload()
    payload["trace"]["steps"] = []
    install_payload(monkeypatch, payload)
    stem, draft = images
    with pytest.raises(ValueError, match="不能为空"):
        await run_diagnosis([stem], [draft])


# ---------------------------------------------------------------------------
# Mock 内核 solver 代码：AST 探针 + 真实可解 + 双盲答案一致
# ---------------------------------------------------------------------------


def test_mock_solver_code_passes_ast_probe() -> None:
    verifier = pytest.importorskip("backend.sandbox.sympy_verifier")
    for purpose in ("scaffold_kernel", "variant_kernel"):
        code = llm_mocks.MOCK_RESPONSES[purpose]["sympy_solver_code"]
        result = verifier.validate_script_ast(code)
        assert result.ok, f"{purpose} AST 探针拒绝: {result.reason}"


@pytest.mark.parametrize("purpose", ["scaffold_kernel", "variant_kernel"])
def test_mock_solver_code_runs_in_real_sandbox(purpose: str) -> None:
    """内核代码在真实受限沙盒中执行并返回契约结构（P2-A 未就位则跳过）。"""
    verifier = pytest.importorskip("backend.sandbox.sympy_verifier")
    code = llm_mocks.MOCK_RESPONSES[purpose]["sympy_solver_code"]
    result = verifier.run_sympy_in_sandbox(code)
    assert result.ok, f"{purpose} 沙盒执行失败: {result.error_kind} {result.stderr_tail}"
    assert result.answer_latex == "x = 5"
    assert isinstance(result.params, dict) and result.params


@pytest.mark.parametrize("purpose", ["scaffold_kernel", "variant_kernel"])
def test_mock_solver_code_actually_solves(purpose: str) -> None:
    fixture = llm_mocks.MOCK_RESPONSES[purpose]
    namespace: dict[str, Any] = {}
    exec(compile(fixture["sympy_solver_code"], f"<{purpose}>", "exec"), namespace)
    result = namespace["solve_kernel"]()
    assert isinstance(result, dict)
    assert isinstance(result["params"], dict)
    assert result["answer_latex"] == "x = 5"  # 真实符号求解结果
    # 模板可被 params 无损回填（str.format 占位符协议）
    rendered = fixture["parametric_stem_tpl"].format(**result["params"])
    assert "{" not in rendered
    for value in result["params"].values():
        assert str(value) in rendered


def test_solver_blind_consistent_with_kernels() -> None:
    blind = llm_mocks.MOCK_RESPONSES["solver_blind"]
    for purpose in ("scaffold_kernel", "variant_kernel"):
        namespace: dict[str, Any] = {}
        code = llm_mocks.MOCK_RESPONSES[purpose]["sympy_solver_code"]
        exec(compile(code, f"<{purpose}>", "exec"), namespace)
        kernel_answer = namespace["solve_kernel"]()["answer_latex"]
        assert blind["answer_latex"].replace(" ", "") == kernel_answer.replace(" ", "")
    assert blind["reasoning_brief"]


def test_mock_purpose_registry_complete() -> None:
    # 四个核心 purpose 必须注册；允许 PM 批准的扩展 purpose（如 P3-A 的 node_judge）
    assert {"diagnosis", "scaffold_kernel", "variant_kernel", "solver_blind"} <= set(
        llm_mocks.MOCK_RESPONSES
    )
    # diagnosis fixture 可被 DiagnosisPayload 直接校验（网关 Mock 路由前提）
    payload = DiagnosisPayload.model_validate(llm_mocks.MOCK_RESPONSES["diagnosis"])
    assert json.dumps(payload.model_dump(), ensure_ascii=False)
