"""diagnose 契约与四维难度上界回归测试（S/T/L∈[1,3]、必填解法、TRAP枚举、SymPy def verify、黄金锚点与网关自愈）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from shuati.config import LLMSlotConfig, Settings
from shuati.db.connection import PreconditionError, get_connection
from shuati.db.schema import init_db
from shuati.llm.prompt_builder import build_external_diagnose_messages
from shuati.models.schemas import DifficultyScoresOutput, ExternalDiagnosisOutput
from shuati.services.mistake import diagnose_external_mistake_async


def _valid_external_diag_payload() -> dict[str, Any]:
    return {
        "visual_facts": "",
        "visual_ambiguity_flag": False,
        "visual_ambiguity_note": "",
        "method_summary": "利用速度比与时间反比建立方程求解",
        "solution_markdown": "设单程距离为 d，满载速度 30，空车速度 90，联立得 d = 90 千米。",
        "final_answers": [
            {
                "label": "主问",
                "answer_type": "NUMERIC",
                "verify_mode": "SYMBOLIC",
                "collection_mode": "SINGLE",
                "answer": "90",
                "unit": "千米",
                "sympy_expr": "90",
                "items": None,
                "choice_label": None,
                "canonical": None,
            }
        ],
        "sympy_code": (
            "import sympy as sp\n"
            "def verify():\n"
            "    d = sp.symbols('d', positive=True)\n"
            "    sol = sp.solve(sp.Eq(5 * (d / 30 + d / 90), 20), d)[0]\n"
            "    return {'主问': str(sol)}\n"
        ),
        "steps": [
            {
                "step_index": 1,
                "sub_goal": "求空车速度并表示单次往返时间",
                "core_formula": "t = d/30 + d/90",
                "hint": "先根据倍数关系求空车速度，再分别表示去程与回程时间。",
                "raw_l1": "行程问题",
                "raw_l2": "比例行程与ST图解法",
                "raw_l3": "速度比与时间反比转换",
            }
        ],
        "abstract_skeleton": "已知往返两段速度倍数关系与多次往返总时间，通过方程或时间反比求单程距离。",
        "primary_l1_module": "行程问题",
        "secondary_l1_modules": [],
        "matched_l2": ["比例行程与ST图解法"],
        "thinking_methods": ["比例转化", "方程建模"],
        "classic_motifs": ["往返行程比例模型"],
        "trap_tags": ["TRAP_FRAME_SHIFT"],
        "difficulty": {
            "C_concept_count": {"evidence": "常规双考点串联（倍数关系与往返时间）", "score": 2},
            "S_chain_length": {"evidence": "两步逻辑推理", "score": 1},
            "T_trickiness": {"evidence": "需区分往返次数与单程", "score": 2},
            "L_computation_load": {"evidence": "简单分数运算", "score": 1},
        },
    }


def test_difficulty_schema_bounds_c4_stl3() -> None:
    # 边界最大值合法：C=4, S=3, T=3, L=3
    valid = DifficultyScoresOutput.model_validate(
        {
            "C_concept_count": {"evidence": "非标高阶构造", "score": 4},
            "S_chain_length": {"evidence": "多分支五步推理", "score": 3},
            "T_trickiness": {"evidence": "强反直觉边界", "score": 3},
            "L_computation_load": {"evidence": "多变量高密度运算", "score": 3},
        }
    )
    assert valid.C_concept_count.score == 4
    assert valid.S_chain_length.score == 3

    # S/T/L 任一维打 4 必须被 Pydantic 直接拒绝
    for dim_key in ("S_chain_length", "T_trickiness", "L_computation_load"):
        payload = {
            "C_concept_count": {"evidence": "常规模型", "score": 2},
            "S_chain_length": {"evidence": "常规步骤", "score": 2},
            "T_trickiness": {"evidence": "常规隐蔽度", "score": 2},
            "L_computation_load": {"evidence": "常规运算", "score": 2},
        }
        payload[dim_key]["score"] = 4
        with pytest.raises(ValidationError):
            DifficultyScoresOutput.model_validate(payload)

    # JSON Schema 中明确暴露 C<=4 与 S/T/L<=3，供网关自愈重试使用
    jschema = DifficultyScoresOutput.model_json_schema()
    defs = jschema["$defs"]
    c_ref = jschema["properties"]["C_concept_count"]["$ref"].split("/")[-1]
    s_ref = jschema["properties"]["S_chain_length"]["$ref"].split("/")[-1]
    assert defs[c_ref]["properties"]["score"]["maximum"] == 4
    assert defs[s_ref]["properties"]["score"]["maximum"] == 3


def test_external_diagnosis_schema_rejects_empty_solution_bad_traps_and_print_sympy() -> None:
    # 1. 空 method_summary / solution_markdown（复现 drf_99e5abd90c01）
    bad_empty = _valid_external_diag_payload()
    bad_empty["method_summary"] = ""
    bad_empty["solution_markdown"] = ""
    with pytest.raises(ValidationError):
        ExternalDiagnosisOutput.model_validate(bad_empty)

    # 2. 自由文本 trap_tags（复现 drf_99e5abd90c01 / drf_2e6e3e9f155d）
    bad_trap = _valid_external_diag_payload()
    bad_trap["trap_tags"] = ["误把已走阶数当作总阶数"]
    with pytest.raises(ValidationError):
        ExternalDiagnosisOutput.model_validate(bad_trap)

    # 3. 无 def verify(): 的 print(...) sympy_code
    bad_sympy = _valid_external_diag_payload()
    bad_sympy["sympy_code"] = "from sympy import symbols\nprint(90)"
    with pytest.raises(ValidationError, match="def verify"):
        ExternalDiagnosisOutput.model_validate(bad_sympy)


def test_external_diagnose_prompt_contains_bounds_rubric_traps_and_anchors() -> None:
    msgs = build_external_diagnose_messages(
        grade_subject="小学奥数",
        question_text="一辆货车往返甲乙两地，求距离。",
        taxonomy_enum_text="- 行程问题: 比例行程与ST图解法",
        anchors_prompt_text="## Stage 1.5 黄金难度锚点标尺\n- Anchor-1 [L1_基础直达] C=1, S=1, T=1, L=1",
    )
    sys_txt = msgs[0]["content"]
    usr_txt = msgs[1]["content"]
    for token in (
        "method_summary",
        "solution_markdown",
        "def verify():",
        "raw_l1",
        "raw_l2",
        "raw_l3",
        "4~14 字脱壳原子转化算子",
        "TRAP_DOMAIN_BOUNDARY",
        "TRAP_FRAME_SHIFT",
        "整数 1~4",
        "整数 1~3，严禁打 4",
    ):
        assert token in sys_txt
    assert "Stage 1.5 黄金难度锚点标尺" in usr_txt
    assert "Anchor-1" in usr_txt


@pytest.mark.asyncio
async def test_diagnose_self_heals_stl_overflow_via_gateway_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """复现 S=4 / T=4 场景：首轮 VLM 返回 S=4、T=4，网关自动附错自愈重试并成功产出草稿。"""
    db_path = tmp_path / "diag.db"
    conn = get_connection(db_path)
    init_db(conn, config_dir=Path("config"))

    settings = Settings()
    settings.pipeline.use_mock_llm = False
    settings.llm.vlm_primary = LLMSlotConfig(
        base_url="https://mock.local/v1",
        api_key="sk-test",
        model="mock-vlm",
        max_concurrency=2,
        rpm_limit=1200,
        max_retries=1,
    )

    bad_payload = _valid_external_diag_payload()
    bad_payload["difficulty"]["S_chain_length"]["score"] = 4
    bad_payload["difficulty"]["T_trickiness"]["score"] = 4

    good_payload = _valid_external_diag_payload()

    responses = [
        httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(bad_payload, ensure_ascii=False)}}]},
        ),
        httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(good_payload, ensure_ascii=False)}}]},
        ),
    ]
    recorded_requests: list[dict[str, Any]] = []

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.is_closed = False

        async def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
            recorded_requests.append(json)
            return responses.pop(0)

        async def aclose(self) -> None:
            self.is_closed = True

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    try:
        res = await diagnose_external_mistake_async(
            conn,
            settings,
            question_text="4 辆汽车分别停在一个十字路口的 4 条岔道上，求最少相聚时间。",
            grade_subject="小学奥数",
            config_dir=Path("config"),
            vectors_dir=tmp_path / "vectors",
            drafts_dir=tmp_path / "drafts",
            use_mock=False,
        )
        assert res["base_elo"] == 1405
        assert res["tier"] == "L3_综合中档"
        assert len(recorded_requests) == 2
        # 确认第 1 次请求注入了黄金锚点，第 2 次自愈请求附带了 schema 报错信息
        assert "Stage 1.5 黄金难度锚点标尺" in recorded_requests[0]["messages"][1]["content"]
        repair_msg = recorded_requests[1]["messages"][-1]["content"]
        assert "less than or equal to 3" in repair_msg

        # 同时验证 --from-json 传入 S=4 时抛 PreconditionError 而非 INTERNAL_ERROR
        with pytest.raises(PreconditionError, match="诊断 JSON 校验失败"):
            await diagnose_external_mistake_async(
                conn,
                settings,
                question_text="测试题干",
                grade_subject="小学奥数",
                diagnosis_json=bad_payload,
                config_dir=Path("config"),
                vectors_dir=tmp_path / "vectors",
                drafts_dir=tmp_path / "drafts",
                use_mock=True,
            )
    finally:
        conn.close()
