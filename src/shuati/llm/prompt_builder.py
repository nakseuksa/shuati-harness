"""字段白名单 Prompt 构造器：严禁 uid / source_collection / chapter / 原始图片路径入 Prompt，内存 EXIF 矫正与多图显式编号。

同时集成三层提示词接缝引擎（seam_loader）并锁死全部硬契约：
- 防泄漏白名单投影；
- `AnswerPart` 映射矩阵与标签规范；
- `def verify():` SymPy AST 规范；
- `visual_facts` 首位与四维难度 `evidence` 先于 `score`；
- D-Audit-4 人工视觉/答案最高权威指令；
- D-Audit-5 `SCAFFOLD` 方向 `source = 子题 → target = 母题`。
"""

from __future__ import annotations

import base64
import copy
import io
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from PIL import Image, ImageOps
from pydantic import BaseModel

from shuati.llm.seam_loader import ResolvedSeamBundle, load_prompt_seams
from shuati.models.schemas import (
    ExternalDiagnosisOutput,
    MergeJudgeBatchOutput,
    MultiSolutionJudgeOutput,
    PrerequisiteDagOutput,
    QualitativeJudgeOutput,
    Stage1Input,
    Stage1PrimaryOutput,
    Stage1SecondaryOutput,
    Stage2OnlyOutput,
    Stage3cBatchMappingOutput,
    Stage3dClusterAssignOutput,
    Stage4RefineBatchOutput,
    TaxonomyProposeOutput,
    UnifiedS1S2Output,
)

STAGE1_PROMPT_VERSION = "s1_v3"
STAGE2_PROMPT_VERSION = "s2_v2"
UNIFIED_PROMPT_VERSION = "unified-v2.0"

_ANSWER_PART_SPEC_TEXT = """`final_answers` 为 `AnswerPart` 非空数组（**严禁改名为 `questions`、`answers` 或其他键**）。每个 `AnswerPart` 必须包含以下字段：
- `label`: 小问标签，仅允许 `"主问"` 或 `"(1)"`, `"(2)"`...；严禁自造 `"(1)-1"`、`"(1)-k"` 等子标签。
- `answer_type` 与 `verify_mode` 严格映射：
  - `NUMERIC` / `EXPRESSION` / `RATIO` / `SET_OR_INTERVAL` -> `verify_mode`: `"SYMBOLIC"`
  - `CHOICE` / `SYMBOL_FILL` -> `verify_mode`: `"EXACT"`
  - `TEXT` / `PROOF` -> `verify_mode`: `"QUALITATIVE"`
- `collection_mode`: `"SINGLE"` | `"ORDERED_TUPLE"` | `"UNORDERED_MULTISET"` | `"LABELED_MAP"`
- **关键约束（必读）**：
  1. 当 `collection_mode == "SINGLE"` 且 `verify_mode == "SYMBOLIC"` 时，`sympy_expr` **必须放在 `AnswerPart` 顶层**且为非空字符串，`items` 必须为 `null`——**严禁把 `sympy_expr` 放进 `items`**！
  2. 当 `collection_mode != "SINGLE"`（即 `ORDERED_TUPLE` / `UNORDERED_MULTISET` / `LABELED_MAP`）时，`items` **必须为非空数组**，`sympy_expr` 置 `null`。注意：**同一未知量存在多个并列可行解（如“3 或 15 分钟”）时必须使用 `"UNORDERED_MULTISET"` 并在 `answer` 中以“或”连接（如 `"3或15"`），严禁误用 `"ORDERED_TUPLE"` 逗号并列**（`"ORDERED_TUPLE"` 仅用于顺序固定的多空序列）。
  3. `CHOICE` 类型（`verify_mode="EXACT"`）时，`choice_label` **必填**非空选项字母（如 `"A"`）。
  4. `SYMBOL_FILL` 类型（`verify_mode="EXACT"`）在 `collection_mode == "SINGLE"` 时，`canonical` **必填**非空标准符号（如 `"＞"`、`"＝"`）。
  5. `TEXT` / `PROOF` 类型（`verify_mode="QUALITATIVE"`）在 `answer` 填写核心结论文本，`sympy_expr`、`items`、`choice_label`、`canonical` 均置 `null`。

`AnswerPart` 典型示例：
- 示例 A（`SINGLE` + `SYMBOLIC`）：
  `{"label": "主问", "answer_type": "NUMERIC", "verify_mode": "SYMBOLIC", "collection_mode": "SINGLE", "answer": "12", "unit": "平方厘米", "sympy_expr": "12", "items": null, "choice_label": null, "canonical": null}`
- 示例 B（多空/多值 `ORDERED_TUPLE` / `UNORDERED_MULTISET` / `LABELED_MAP`）：
  - `ORDERED_TUPLE`：`{"label": "(1)", "answer_type": "EXPRESSION", "verify_mode": "SYMBOLIC", "collection_mode": "ORDERED_TUPLE", "answer": "20, -, 5", "unit": null, "sympy_expr": null, "items": [{"item_type": "NUMERIC", "expr": "20"}, {"item_type": "SYMBOL_FILL", "canonical": "－"}, {"item_type": "NUMERIC", "expr": "5"}], "choice_label": null, "canonical": null}`
  - `UNORDERED_MULTISET`：`items` 如 `[{"expr": "3"}, {"expr": "5"}, {"expr": "5"}]`
  - `LABELED_MAP`：`items` 如 `[{"label": "鸡", "expr": "12"}, {"label": "兔", "expr": "8"}]`
- 示例 C（`CHOICE` / `SYMBOL_FILL` / `QUALITATIVE`）：
  - `CHOICE`：`{"label": "主问", "answer_type": "CHOICE", "verify_mode": "EXACT", "collection_mode": "SINGLE", "answer": "B", "unit": null, "sympy_expr": null, "items": null, "choice_label": "B", "canonical": null}`
  - `SYMBOL_FILL`：`{"label": "(2)", "answer_type": "SYMBOL_FILL", "verify_mode": "EXACT", "collection_mode": "SINGLE", "answer": "＞", "unit": null, "sympy_expr": null, "items": null, "choice_label": null, "canonical": "＞"}`
  - `TEXT`/`PROOF`：`{"label": "(2)", "answer_type": "PROOF", "verify_mode": "QUALITATIVE", "collection_mode": "SINGLE", "answer": "由全等三角形判定 SAS 可得结论成立", "unit": null, "sympy_expr": null, "items": null, "choice_label": null, "canonical": null}`"""

_TRAP_ENUM_SPEC_TEXT = """`trap_tags` **严禁自造中文自由文本**，仅允许从以下 10 个标准代码中选取（优先选最贴切的中粒度子类，无刻意陷阱则置空数组 `[]`）：
   - `TRAP_DOMAIN_BOUNDARY`（边界端点、分段折返区间归属与定义域遗漏）
   - `TRAP_SIGN_DIRECTION`（符号方向与顺逆/正负号歧义）
   - `TRAP_EXTRANEOUS_ROOT`（增根虚解、负时间与几何/整数不可行）
   - `TRAP_FRAME_SHIFT`（运动载体参考系转换与百分比/面积基准量漂移）
   - `TRAP_APPROX_CONDITION`（近似条件滥用、中间步过早取整与循环论证）
   - `TRAP_ARITHMETIC_SLIP`（纯四则计算与抄写笔误，严禁因常规运算滥标）
   - `TRAP_AVERAGE_HARMONIC`（等距往返/上下坡将调和平均速度误算为算术平均）
   - `TRAP_EXTENDED_BODY_LENGTH`（有长物体完全过桥加车长、整车在桥减车长、双车长和与车上人看单车长混淆）
   - `TRAP_ASYNC_SYNC_CONFUSION`（错时/起点偏移出发却盲目套用同时出发 1S:3S:5S 等同步倍率公式）
   - `TRAP_DIRECTION_REF_POINT`（折返后混淆距A地/距B地/距中点、已走与剩余路程、或迎面速度和与同向速度差）"""

_DIFFICULTY_RUBRIC_SPEC_TEXT = """四维难度 `difficulty` 必须按学段最优标准解法并严格对照下方黄金锚点打分，每维**先写 `evidence`（可观测解题动作证据）再写整数 `score`**，严禁超界：
   - `C_concept_count`（概念与思维深度，**整数 1~4**）：1=单模型直推；2=单点变形或常规双考点串联；3=跨模块综合或隐式深层建模；4=非标构造或高阶竞赛洞察。
   - `S_chain_length`（逻辑推理链长，不含纯四则算术，**整数 1~3，严禁打 4**）：1=1~2 逻辑步；2=3~4 逻辑步；3=≥5 步或 ≥2 种独立情况分类讨论。
   - `T_trickiness`（信息隐蔽度与陷阱，**整数 1~3，严禁打 4**）：1=显式透明；2=含冗余干扰/隐含条件或常规图文转译；3=强反直觉、极端临界边界或连环陷阱。
   - `L_computation_load`（运算与审题负荷，**整数 1~3，严禁打 4**）：1=心算/短题干；2=常规纸笔多步演算；3=高繁复计算、多变量联立或高密度信息。"""

_MIME_BY_FORMAT: dict[str, str] = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
    "BMP": "image/bmp",
}


def _try_load_seam_bundle(
    *,
    grade_subject: str,
    config_dir: Path,
    prompt_profile: str | None,
) -> ResolvedSeamBundle | None:
    if prompt_profile is not None and prompt_profile.strip():
        return load_prompt_seams(
            config_dir=config_dir,
            grade_subject=grade_subject,
            profile=prompt_profile,
        )
    default_yaml = Path(config_dir) / "prompts" / "default.yaml"
    if default_yaml.is_file():
        return load_prompt_seams(
            config_dir=config_dir,
            grade_subject=grade_subject,
            profile=None,
        )
    repo_config_dir = Path(__file__).resolve().parents[3] / "config"
    if (repo_config_dir / "prompts" / "default.yaml").is_file():
        return load_prompt_seams(
            config_dir=repo_config_dir,
            grade_subject=grade_subject,
            profile=None,
        )
    return None


def _join_seam_sections(bundle: ResolvedSeamBundle | None, keys: Sequence[str], *, section: str = "blocks") -> str:
    if bundle is None:
        return ""
    mapping = bundle.blocks if section == "blocks" else bundle.stages
    parts = [mapping[k].strip() for k in keys if k in mapping and mapping[k].strip()]
    return "\n\n".join(parts)


def _load_transposed_image_data_url(image_path: Path) -> str:
    """读取 CAS 图片，在内存中执行 ImageOps.exif_transpose() 矫正方向并编码为 base64 data URL。"""
    with Image.open(image_path) as raw_img:
        raw_fmt = (raw_img.format or "PNG").upper()
        if raw_fmt == "JPG":
            raw_fmt = "JPEG"
        if raw_fmt not in _MIME_BY_FORMAT:
            raw_fmt = "PNG"

        transposed: Image.Image = ImageOps.exif_transpose(raw_img)
        if raw_fmt == "JPEG" and transposed.mode in ("RGBA", "P", "LA"):
            transposed = transposed.convert("RGB")

        buf = io.BytesIO()
        transposed.save(buf, format=raw_fmt)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        mime = _MIME_BY_FORMAT[raw_fmt]
        return f"data:{mime};base64,{b64}"


def _resolve_local_image_path(img_ref: str | Path, assets_dir: Path | None) -> Path:
    p = Path(img_ref)
    if assets_dir is not None and not p.is_absolute():
        direct = p
        under_assets = Path(assets_dir) / p
        if under_assets.exists() or not direct.exists():
            return under_assets
    return p


def _append_image_blocks(
    user_parts: list[dict[str, Any]],
    *,
    images: Sequence[str | Path],
    captions: Sequence[str],
    embed_images: bool,
    assets_dir: Path | None,
    always_label: bool = False,
) -> None:
    multi = len(images) >= 2
    for idx, img_ref in enumerate(images, start=1):
        cap = captions[idx - 1].strip() if idx - 1 < len(captions) else ""
        if multi or always_label:
            label_text = f"【配图 {idx}（{cap}）】" if cap else f"【配图 {idx}】"
            user_parts.append({"type": "text", "text": label_text})
        elif cap:
            user_parts.append({"type": "text", "text": f"【配图 {idx}（{cap}）】"})

        if embed_images:
            resolved = _resolve_local_image_path(img_ref, assets_dir)
            data_url = _load_transposed_image_data_url(resolved)
            user_parts.append({"type": "image_url", "image_url": {"url": data_url}})
        else:
            user_parts.append(
                {
                    "type": "image_path",
                    "image_path": str(img_ref),
                    "index": idx,
                    "caption": cap,
                }
            )


def flatten_json_schema(schema_cls: type[BaseModel]) -> dict[str, Any]:
    """内联展开 Pydantic `$defs`/`$ref` 并锁死 Stage 2 四维难度硬界限（C∈[1,4]、S/T/L∈[1,3]）。"""
    raw_schema = copy.deepcopy(schema_cls.model_json_schema())
    defs: dict[str, Any] = {}
    for def_key in ("$defs", "definitions"):
        extracted = raw_schema.pop(def_key, None)
        if isinstance(extracted, dict):
            defs.update(extracted)

    def _resolve_ref(ref_str: str, active_stack: set[str]) -> Any:
        if not ref_str.startswith("#/"):
            return {"$ref": ref_str}
        parts = ref_str[2:].split("/")
        if len(parts) == 2 and parts[0] in ("$defs", "definitions") and parts[1] in defs:
            name = parts[1]
            if name in active_stack:
                return {"type": "object"}
            return _inline(copy.deepcopy(defs[name]), active_stack | {name})
        return {"$ref": ref_str}

    def _inline(node: Any, active_stack: set[str]) -> Any:
        if isinstance(node, list):
            return [_inline(item, active_stack) for item in node]
        if not isinstance(node, dict):
            return node

        if "$ref" in node:
            ref_target = _resolve_ref(str(node["$ref"]), active_stack)
            siblings = {k: v for k, v in node.items() if k != "$ref"}
            if isinstance(ref_target, dict):
                merged = copy.deepcopy(ref_target)
                for k, v in siblings.items():
                    merged[k] = _inline(v, active_stack)
                return _inline(merged, active_stack)
            return ref_target

        out: dict[str, Any] = {}
        for k, v in node.items():
            if k in ("$defs", "definitions"):
                continue
            out[k] = _inline(v, active_stack)
        return out

    def _enforce_hard_bounds(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                _enforce_hard_bounds(item)
            return
        if not isinstance(node, dict):
            return

        props = node.get("properties")
        if isinstance(props, dict):
            if "visual_facts" in props:
                reordered: dict[str, Any] = {"visual_facts": props["visual_facts"]}
                for pk, pv in props.items():
                    if pk != "visual_facts":
                        reordered[pk] = pv
                node["properties"] = reordered
                props = reordered

            for dim_key, max_val in (
                ("C_concept_count", 4),
                ("S_chain_length", 3),
                ("T_trickiness", 3),
                ("L_computation_load", 3),
            ):
                dim_schema = props.get(dim_key)
                if isinstance(dim_schema, dict) and isinstance(dim_schema.get("properties"), dict):
                    d_props = dim_schema["properties"]
                    if "evidence" in d_props and "score" in d_props:
                        dim_schema["properties"] = {
                            "evidence": d_props["evidence"],
                            "score": d_props["score"],
                            **{k: v for k, v in d_props.items() if k not in ("evidence", "score")},
                        }
                        dim_schema["required"] = ["evidence", "score"]
                    score_prop = dim_schema["properties"].get("score")
                    if isinstance(score_prop, dict):
                        score_prop["type"] = "integer"
                        score_prop["minimum"] = 1
                        score_prop["maximum"] = max_val

        for v in node.values():
            _enforce_hard_bounds(v)

    flattened = _inline(raw_schema, set())
    if isinstance(flattened, dict):
        _enforce_hard_bounds(flattened)
        return flattened
    return {}


def build_stage1_messages(
    inp: Stage1Input,
    *,
    role: Literal["PRIMARY", "SECONDARY"],
    assets_dir: Path,
    config_dir: Path = Path("config"),
    prompt_profile: str | None = None,
) -> list[dict[str, Any]]:
    """严格字段白名单组装 Stage 1 消息。

    - 严禁将 inp.question_uid、source_collection、chapter、ch\\d+_、legacy_tags、原始图片路径写入任何 message；
    - 有图时从 assets_dir / cas_img 读取，内存执行 ImageOps.exif_transpose()，转 base64 data URL；
    - len(cas_images) >= 2 时，每张图前插入文本块：【配图 {i}】或【配图 {i}（{caption}）】；
    - 明确声明：禁止自造 (1)-k 子小问标签；未指定 π 近似值时保留精确符号 pi；visual_facts 严禁写入未画出的辅助线或推导结论。
    """
    bundle = _try_load_seam_bundle(
        grade_subject=inp.grade_subject,
        config_dir=config_dir,
        prompt_profile=prompt_profile,
    )
    seam_notes = _join_seam_sections(bundle, ("persona", "visual_extraction", "solving_conventions"))

    role_desc = (
        "你是主路独立解题与验真专家（PRIMARY），需在解答末尾给出可独立运行的 Python SymPy 验算脚本 sympy_code（定义 def verify(): 返回 dict[str, str | list[str]]）。"
        if role == "PRIMARY"
        else "你是辅路双盲独立解题专家（SECONDARY），请完全独立读题看图并解答，严禁猜测他人解法。"
    )

    sympy_rule = (
        "\n6. `sympy_code` 规范（存在 `verify_mode == \"SYMBOLIC\"` 小问时必填非空字符串，否则置 `null`）："
        "仅允许导入 sympy / math / fractions；严禁调用 sympify / parse_expr / lambdify / S(<字符串>) / eval / exec / open；"
        "必须定义 def verify(): 返回字典，键为归一后的小问标签（如 '主问' 或 '(1)'），值必须经由真实数学运算得出（禁止硬编码常量返回）。"
        if role == "PRIMARY"
        else ""
    )

    if role == "PRIMARY":
        json_schema_template = (
            "{\n"
            '  "visual_facts": "客观视觉事实描述（必须排在首位，无图题置空字符串 \\"\\"）",\n'
            '  "visual_ambiguity_flag": false,\n'
            '  "visual_ambiguity_note": "",\n'
            '  "method_summary": "一句话解法路线",\n'
            '  "solution_markdown": "完整推导过程（Markdown/LaTeX）",\n'
            '  "final_answers": [ ... ],\n'
            '  "sympy_code": "存在 SYMBOLIC 小问时必填 Python 验算脚本，否则为 null"\n'
            "}"
        )
        top_keys_desc = (
            "顶层必填键（PRIMARY）：`visual_facts`（必须排首位，str）、`visual_ambiguity_flag`（bool）、"
            "`visual_ambiguity_note`（str）、`method_summary`（str，一句话解法路线）、"
            "`solution_markdown`（str，完整推导过程）、`final_answers`（AnswerPart 非空数组，严禁改名为 `questions` 或其他键）、"
            "`sympy_code`（str | null，存在 SYMBOLIC 小问时必填，否则为 null）。"
        )
    else:
        json_schema_template = (
            "{\n"
            '  "visual_facts": "客观视觉事实描述（必须排在首位，无图题置空字符串 \\"\\"）",\n'
            '  "visual_ambiguity_flag": false,\n'
            '  "visual_ambiguity_note": "",\n'
            '  "method_summary": "一句话解法路线",\n'
            '  "solution_markdown": "完整推导过程（Markdown/LaTeX）",\n'
            '  "final_answers": [ ... ]\n'
            "}"
        )
        top_keys_desc = (
            "顶层必填键（SECONDARY，不含 `sympy_code` 字段）：`visual_facts`（必须排首位，str）、"
            "`visual_ambiguity_flag`（bool）、`visual_ambiguity_note`（str）、`method_summary`（str，一句话解法路线）、"
            "`solution_markdown`（str，完整推导过程）、`final_answers`（AnswerPart 非空数组，严禁改名为 `questions` 或其他键）。"
        )

    seam_section = f"\n\n【学科教研补充规范】\n{seam_notes}" if seam_notes else ""

    system_text = (
        f"你是一位严谨的{inp.grade_subject}学科解题专家。{role_desc}\n"
        "请严格输出一个合法的 JSON 对象（不要输出 Markdown 代码块包裹），并遵守以下硬性规则：\n"
        "1. JSON 字段顺序中 `visual_facts` 必须排在首位！无图题置空字符串 \"\"；"
        "有图时仅客观描述图中可见元素，严禁写入未画出的辅助线或推导结论，歧义时置 `visual_ambiguity_flag=true` 并在 `visual_ambiguity_note` 说明。\n"
        "2. 小问标签归一规范：题干有显式编号时统一使用 \"(1)\", \"(2)\"...；单问使用 \"主问\"；"
        "无编号“计算+说理”复合问拆为 \"(1)\" 与 \"(2)\"；严禁自造 \"(1)-1\"、\"(1)-k\" 等子小问标签！\n"
        "3. 单值小问使用 `collection_mode: \"SINGLE\"`（此时 `items` 为 `null`，且若为 `SYMBOLIC` 则 `sympy_expr` 必须放在 `AnswerPart` 顶层）；"
        "同一小问多空或多值使用 `ORDERED_TUPLE` / `UNORDERED_MULTISET` / `LABELED_MAP` 并装入非空 `items` 数组。\n"
        "4. `answer_type` 与 `verify_mode` 必须严格匹配："
        "NUMERIC/EXPRESSION/RATIO/SET_OR_INTERVAL -> SYMBOLIC；CHOICE/SYMBOL_FILL -> EXACT；TEXT/PROOF -> QUALITATIVE。\n"
        "5. 数值约定：题干未明确指定 π 近似值（如“π 取 3.14”）时，`sympy_expr` 必须保留精确符号 pi、sqrt(...) 或分数。"
        f"{sympy_rule}{seam_section}\n\n"
        "【输出 JSON 结构模板】\n"
        f"{top_keys_desc}\n"
        f"{json_schema_template}\n\n"
        "【`final_answers` 元素 `AnswerPart` 规范】\n"
        f"{_ANSWER_PART_SPEC_TEXT}"
    )

    user_parts: list[dict[str, Any]] = []
    text_lines = [f"【学段学科】{inp.grade_subject}", f"【题干】\n{inp.question_text}"]
    if inp.options:
        opts_json = json.dumps(inp.options, ensure_ascii=False)
        text_lines.append(f"【选择题选项】{opts_json}")
    user_parts.append({"type": "text", "text": "\n\n".join(text_lines)})

    images = inp.cas_images or []
    captions = inp.image_captions or []
    _append_image_blocks(
        user_parts,
        images=images,
        captions=captions,
        embed_images=True,
        assets_dir=Path(assets_dir),
    )

    user_content: Any = user_parts if images else user_parts[0]["text"]
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_content},
    ]


def build_qualitative_judge_messages(
    label: str,
    answer_a: str,
    answer_b: str,
    question_text: str = "",
    *,
    grade_subject: str = "小学奥数",
    config_dir: Path = Path("config"),
    prompt_profile: str | None = None,
) -> list[dict[str, Any]]:
    """构造定性/作图小问语义等价裁判消息（纯文本，字段白名单）。"""
    bundle = _try_load_seam_bundle(
        grade_subject=grade_subject,
        config_dir=config_dir,
        prompt_profile=prompt_profile,
    )
    judge_seam = _join_seam_sections(bundle, ("stage1_judge",), section="stages")
    seam_tail = f"\n{judge_seam}" if judge_seam else ""

    system_text = (
        "你是严谨的理科阅卷裁判。请判断两位解题者对同一小问的定性表述或作图操作是否在核心科学结论与数理依据上等价。\n"
        "请严格输出 JSON：{\"equivalent\": bool, \"confidence\": \"HIGH\"|\"MEDIUM\"|\"LOW\", \"reason\": \"...\"}。"
        f"{seam_tail}"
    )
    sections = [f"【小问标签】{label}"]
    if question_text.strip():
        sections.append(f"【题干参考】\n{question_text.strip()}")
    sections.append(f"【答案 A】\n{answer_a}")
    sections.append(f"【答案 B】\n{answer_b}")
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": "\n\n".join(sections)},
    ]


def build_multi_solution_judge_messages(
    summary_a: str,
    sol_a: str,
    summary_b: str,
    sol_b: str,
    *,
    grade_subject: str = "小学奥数",
    config_dir: Path = Path("config"),
    prompt_profile: str | None = None,
) -> list[dict[str, Any]]:
    """构造一题多解本质路线差异裁判消息（纯文本，字段白名单）。"""
    bundle = _try_load_seam_bundle(
        grade_subject=grade_subject,
        config_dir=config_dir,
        prompt_profile=prompt_profile,
    )
    judge_seam = _join_seam_sections(bundle, ("stage1_judge",), section="stages")
    seam_tail = f"\n{judge_seam}" if judge_seam else ""

    system_text = (
        "你是竞赛数学教研专家。请判断以下两份正确解答是否属于本质不同的解题路线（跨核心数学范式/模型），"
        "而非伪多解差异。\n"
        "请严格输出 JSON：{\"is_distinct_method\": bool, \"reason\": \"...\"}。"
        f"{seam_tail}"
    )
    user_text = (
        f"【解法 A 摘要】{summary_a}\n【解法 A 正文】\n{sol_a}\n\n"
        f"【解法 B 摘要】{summary_b}\n【解法 B 正文】\n{sol_b}"
    )
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]


def build_review_resolve_messages(
    question_text: str,
    visual_facts: str,
    grade_subject: str,
    *,
    config_dir: Path = Path("config"),
    prompt_profile: str | None = None,
) -> list[dict[str, Any]]:
    """构造复核页 [R] 辅助重算参考解答草稿消息（纯文本 text_llm，零视觉 Token，字段白名单）。"""
    bundle = _try_load_seam_bundle(
        grade_subject=grade_subject,
        config_dir=config_dir,
        prompt_profile=prompt_profile,
    )
    seam_notes = _join_seam_sections(bundle, ("solving_conventions",))
    seam_tail = f"\n\n【学科求解补充】\n{seam_notes}" if seam_notes else ""

    system_text = (
        f"你是{grade_subject}学科解答重算助手。人工复核员已核定该题的题干与客观视觉事实（visual_facts），"
        "请严格依据提供的题干与 visual_facts 重新推导完整参考解答、各小问结构化答案（final_answers）与 SymPy 验算脚本（sympy_code）。\n"
        "请严格输出一个合法的 JSON 对象（不要输出 Markdown 代码块包裹），遵守以下要求：\n"
        "1. 小问标签仅允许 \"主问\" 或 \"(1)\", \"(2)\"...，禁止自造 \"(1)-1\" 子标签。\n"
        "2. `sympy_code`（存在 `SYMBOLIC` 小问时必填，否则置 `null`）：仅允许导入 sympy / math / fractions，"
        "必须定义 def verify(): 返回 dict[str, str | list[str]]，并通过真实运算得出结果（禁止硬编码常量返回）。"
        f"{seam_tail}\n\n"
        "【输出 JSON 结构模板】\n"
        "顶层必填键：`method_summary`（str，一句话解法路线）、`solution_markdown`（str，完整推导过程）、"
        "`final_answers`（AnswerPart 非空数组，严禁改名为 `questions` 或其他键）、"
        "`sympy_code`（str | null）。\n"
        "{\n"
        '  "method_summary": "一句话解法路线",\n'
        '  "solution_markdown": "完整推导过程（Markdown/LaTeX）",\n'
        '  "final_answers": [ ... ],\n'
        '  "sympy_code": "存在 SYMBOLIC 小问时必填 Python 验算脚本，否则为 null"\n'
        "}\n\n"
        "【`final_answers` 元素 `AnswerPart` 规范】\n"
        f"{_ANSWER_PART_SPEC_TEXT}"
    )
    user_text = f"【学段学科】{grade_subject}\n\n【题干】\n{question_text}\n\n【已核定视觉事实 visual_facts】\n{visual_facts or '（无配图）'}"
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]


def build_stage2_messages(
    *,
    grade_subject: str,
    question_text: str,
    visual_facts: str = "",
    verified_final_answers: list[dict[str, Any]] | None = None,
    verified_method_summary: str = "",
    verified_solution_markdown: str = "",
    anchors_prompt_text: str = "",
    taxonomy_enum_text: str = "",
    options: dict[str, str] | None = None,
    has_images: bool = False,
    image_paths: Sequence[str | Path] | None = None,
    image_captions: Sequence[str] | None = None,
    assets_dir: Path | None = None,
    embed_images: bool = True,
    has_human_visual_override: bool = False,
    has_human_answer_override: bool = False,
    task_id: str | None = None,
    config_dir: Path = Path("config"),
    prompt_profile: str | None = None,
) -> list[dict[str, Any]]:
    """Stage 2 多模态深度拆解与四维打标消息构造（D-Audit-4：有图传原图 + 人工视觉/答案最高权威）。"""
    bundle = _try_load_seam_bundle(
        grade_subject=grade_subject,
        config_dir=config_dir,
        prompt_profile=prompt_profile,
    )
    block_keys = ["persona", "steps_and_socratic_hint", "abstraction_and_tagging", "difficulty_rubric_notes"]
    if has_images:
        block_keys.insert(1, "visual_extraction")
    seam_notes = _join_seam_sections(bundle, block_keys)

    override_rules: list[str] = []
    if has_human_visual_override:
        override_rules.append(
            "【D-Audit-4 人工视觉事实最高权威】本题的 `verified_visual_facts` 已由人工复核核定；"
            "若原图印刷模糊、阴影边界或标注细节与已核定 `verified_visual_facts` 存在歧义冲突，"
            "**必须无条件以人工核定的 `verified_visual_facts` 为准，严禁自行推翻重读**。"
        )
    if has_human_answer_override:
        override_rules.append(
            "【D-Audit-4 人工标准答案最高权威】本题的 `verified_final_answers` 已由人工复核核定；"
            "步骤拆解、核心公式与提示**必须无条件服从该人工标准答案**，严禁篡改答案或得出相反结论。"
        )
    if not override_rules:
        override_rules.append(
            "【已验真事实约束】本题已通过 Stage 1 验真，所有拆解步骤与难度评估必须忠于已核定的参考解答与标准答案，不得自行推翻终值。"
        )

    seam_section = f"\n\n【学科与阶段指导】\n{seam_notes}" if seam_notes else ""
    override_section = "\n".join(override_rules)

    system_text = (
        f"你是严谨的{grade_subject}学科题库深度拆解与难度标定专家（Stage 2）。\n"
        f"{override_section}\n\n"
        "请严格输出一个合法的 JSON 对象（不要输出 Markdown 代码块包裹），并遵守以下硬性结构契约（教研细则见下方【学科与阶段指导】）：\n"
        "1. `steps`（1~5 个有教学意义的逻辑推理步，`step_index` 从 1 连续递增）：\n"
        "   - 必填 `sub_goal`、`core_formula`、`hint`（设问式零泄密，严禁剧透得数）、`raw_l1`、`raw_l2`；\n"
        "   - `raw_l3`: **4~14 字脱壳原子转化算子短语**，严禁包含题干具体数字（含汉字数字参数）、人名道具或整句解说。\n"
        "2. `abstract_skeleton`（三段式去情境抽象骨架）、`primary_l1_module`、`secondary_l1_modules`、`thinking_methods`（1~3 个具体可操作技法，禁用万金油泛词）、`classic_motifs`（仅公认经典母题原型，未命中置 `[]`）。\n"
        f"3. {_TRAP_ENUM_SPEC_TEXT}\n"
        f"4. {_DIFFICULTY_RUBRIC_SPEC_TEXT}{seam_section}\n\n"
        "【输出 JSON 结构模板】\n"
        "{\n"
        '  "task_id": "原样回填 task_id",\n'
        '  "steps": [{"step_index": 1, "sub_goal": "...", "core_formula": "...", "hint": "...", "raw_l1": "...", "raw_l2": "...", "raw_l3": "4~14字原子算子"}],\n'
        '  "abstract_skeleton": "去情境抽象骨架（无具体数字）",\n'
        '  "primary_l1_module": "一级大模块",\n'
        '  "secondary_l1_modules": [],\n'
        '  "thinking_methods": ["具体技法"],\n'
        '  "classic_motifs": ["经典母题"],\n'
        '  "trap_tags": [],\n'
        '  "difficulty": {\n'
        '    "C_concept_count": {"evidence": "...", "score": 2},\n'
        '    "S_chain_length": {"evidence": "...", "score": 2},\n'
        '    "T_trickiness": {"evidence": "...", "score": 2},\n'
        '    "L_computation_load": {"evidence": "...", "score": 1}\n'
        "  }\n"
        "}"
    )

    sections: list[str] = []
    if task_id:
        sections.append(f"【task_id】{task_id}")
    sections.append(f"【学段学科】{grade_subject}")
    if taxonomy_enum_text.strip():
        sections.append(f"【当前标准词表参考】\n{taxonomy_enum_text.strip()}")
    if anchors_prompt_text.strip():
        sections.append(f"【难度校准黄金锚点标尺】\n{anchors_prompt_text.strip()}")
    sections.append(f"【题干】\n{question_text}")
    if options:
        sections.append(f"【选择题选项】{json.dumps(options, ensure_ascii=False)}")
    if visual_facts.strip() or has_human_visual_override:
        vf_label = "【人工核定视觉事实 visual_facts（最高权威）】" if has_human_visual_override else "【已核定视觉事实 visual_facts】"
        sections.append(f"{vf_label}\n{visual_facts.strip() or '（无配图）'}")
    if verified_final_answers is not None:
        ans_label = "【人工核定标准答案 final_answers（最高权威）】" if has_human_answer_override else "【已验真标准答案 final_answers】"
        sections.append(f"{ans_label}\n{json.dumps(verified_final_answers, ensure_ascii=False)}")
    if verified_method_summary.strip():
        sections.append(f"【已验真解法摘要】{verified_method_summary.strip()}")
    if verified_solution_markdown.strip():
        sections.append(f"【已验真参考解答】\n{verified_solution_markdown.strip()}")

    user_parts: list[dict[str, Any]] = [{"type": "text", "text": "\n\n".join(sections)}]
    imgs = list(image_paths or []) if has_images else []
    caps = list(image_captions or [])
    if imgs:
        _append_image_blocks(
            user_parts,
            images=imgs,
            captions=caps,
            embed_images=embed_images,
            assets_dir=assets_dir,
            always_label=not embed_images,
        )

    user_content: Any = user_parts if imgs else user_parts[0]["text"]
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_content},
    ]


def build_unified_s1_s2_messages(
    *,
    task_payload: dict[str, Any],
    anchors_prompt_text: str,
    config_dir: Path = Path("config"),
    prompt_profile: str | None = None,
    embed_images: bool = False,
    assets_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], ResolvedSeamBundle]:
    """构造 Stage 1+2 合一编译器（或 STAGE2_ONLY）脱敏消息，返回 `(messages, ResolvedSeamBundle)`。"""
    # 严格白名单投影：绝不读取 question_uid / source_collection / raw_chapter / legacy_tags / raw_images
    task_id = str(task_payload.get("task_id") or "")
    mode = str(task_payload.get("mode") or "UNIFIED_S1_S2").strip().upper()
    grade_subject = str(task_payload.get("grade_subject") or "小学奥数")
    allow_pi_314 = bool(task_payload.get("allow_pi_314", False))
    question_text = str(task_payload.get("question_text") or "")
    q_format = str(task_payload.get("format") or "")
    options = task_payload.get("options") if isinstance(task_payload.get("options"), dict) else {}
    raw_images = task_payload.get("image_paths") if task_payload.get("image_paths") is not None else task_payload.get("cas_images")
    images = [str(p) for p in raw_images] if isinstance(raw_images, list) else []
    has_images = bool(task_payload.get("has_images", False) or len(images) > 0)
    raw_caps = task_payload.get("image_captions")
    captions = [str(c) for c in raw_caps] if isinstance(raw_caps, list) else []

    bundle = load_prompt_seams(
        config_dir=config_dir,
        grade_subject=grade_subject,
        profile=prompt_profile,
    )

    if mode == "STAGE2_ONLY":
        verified_vf = str(task_payload.get("verified_visual_facts") or "")
        verified_ans = task_payload.get("verified_final_answers")
        verified_answers_list = verified_ans if isinstance(verified_ans, list) else None
        msgs = build_stage2_messages(
            grade_subject=grade_subject,
            question_text=question_text,
            visual_facts=verified_vf,
            verified_final_answers=verified_answers_list,
            verified_method_summary=str(task_payload.get("verified_method_summary") or ""),
            verified_solution_markdown=str(task_payload.get("verified_solution_markdown") or ""),
            anchors_prompt_text=anchors_prompt_text,
            options=options or None,
            has_images=has_images,
            image_paths=images,
            image_captions=captions,
            assets_dir=assets_dir,
            embed_images=embed_images,
            has_human_visual_override=bool(task_payload.get("has_human_visual_override", False)),
            has_human_answer_override=bool(task_payload.get("has_human_answer_override", False)),
            task_id=task_id,
            config_dir=config_dir,
            prompt_profile=prompt_profile,
        )
        return msgs, bundle

    seam_notes = _join_seam_sections(
        bundle,
        (
            "persona",
            "visual_extraction",
            "solving_conventions",
            "steps_and_socratic_hint",
            "abstraction_and_tagging",
            "difficulty_rubric_notes",
        ),
    )

    pi_rule = (
        "题干明确写明“π 取 3.14”等近似值时按指定近似值计算；未指定时 `sympy_expr` 必须保留精确符号 `pi`、`sqrt(...)` 或分数。"
        if allow_pi_314
        else "除非题干显式要求近似值，否则 `sympy_expr` 一律保留精确符号 `pi`、`sqrt(...)` 或分数。"
    )

    system_text = (
        f"你是严谨的{grade_subject}学科知识编译器专家（UNIFIED_S1_S2 合一求解与深度拆解）。\n"
        "请严格输出一个合法的 JSON 对象（不要输出 Markdown 代码块包裹），并遵守以下全部硬性契约（教研细则见下方【学科与接缝规范】）：\n"
        "1. `visual_facts` 必须置于顶层内容字段首位（无图题置 `\"\"`），仅客观描述可见元素，严禁写入未画出的辅助线或推导结论；"
        "仅在原图模糊或边界/标注存疑时设 `visual_ambiguity_flag=true` 并在 `visual_ambiguity_note` 说明。\n"
        "2. `method_summary`（≥2字）与 `solution_markdown`（≥5字）必填非空。\n"
        "3. `sympy_code`（存在 `verify_mode == \"SYMBOLIC\"` 小问时必填，否则置 `null`）：仅允许导入 sympy / math / fractions；"
        "严禁调用 sympify / parse_expr / lambdify / S(<字符串>) / eval / exec / open；"
        "**必须定义 `def verify():`** 返回字典（键为归一后小问标签如 `'主问'` 或 `'(1)'`，值为通过真实运算得出的结果，禁止硬编码常量返回）。\n"
        f"4. 数值与标签规范：{pi_rule} 小问标签仅允许 `\"主问\"` 或 `\"(1)\"`, `\"(2)\"`...，禁止自造 `\"(1)-1\"` 或 `\"(1)-k\"`。\n"
        "5. `steps`（1~5 步，`step_index` 从 1 连续递增）：`hint` 设问式零泄密；`raw_l3` 必须是 **4~14 字脱壳原子转化算子短语**，不含具体数字或人名道具。\n"
        "6. `abstract_skeleton`、`thinking_methods`（1~3 个具体技法，禁用万金油泛词）、`classic_motifs`（未命中置 `[]`）严格遵循下方接缝规范。\n"
        f"7. {_TRAP_ENUM_SPEC_TEXT}\n"
        f"8. {_DIFFICULTY_RUBRIC_SPEC_TEXT}\n\n"
        f"【学科与接缝规范（digest={bundle.prompt_digest}）】\n"
        f"{seam_notes}\n\n"
        "【输出 JSON 顶层结构模板（`visual_facts` 置于内容字段首位）】\n"
        "{\n"
        '  "visual_facts": "",\n'
        '  "task_id": "原样回填 task_id",\n'
        '  "visual_ambiguity_flag": false,\n'
        '  "visual_ambiguity_note": "",\n'
        '  "method_summary": "一句话解法路线",\n'
        '  "solution_markdown": "完整推导过程（Markdown/LaTeX）",\n'
        '  "final_answers": [ ... ],\n'
        '  "sympy_code": "def verify():\\n    ...\\n    return {\'主问\': str(ans)}\\n",\n'
        '  "steps": [{"step_index": 1, "sub_goal": "...", "core_formula": "...", "hint": "...", "raw_l1": "...", "raw_l2": "...", "raw_l3": "4~14字原子算子"}],\n'
        '  "abstract_skeleton": "去情境抽象骨架（无具体数字）",\n'
        '  "primary_l1_module": "一级大模块",\n'
        '  "secondary_l1_modules": [],\n'
        '  "thinking_methods": ["具体技法"],\n'
        '  "classic_motifs": ["经典母题"],\n'
        '  "trap_tags": [],\n'
        '  "difficulty": {\n'
        '    "C_concept_count": {"evidence": "...", "score": 2},\n'
        '    "S_chain_length": {"evidence": "...", "score": 2},\n'
        '    "T_trickiness": {"evidence": "...", "score": 2},\n'
        '    "L_computation_load": {"evidence": "...", "score": 1}\n'
        "  }\n"
        "}\n\n"
        "【`final_answers` 元素 `AnswerPart` 规范】\n"
        f"{_ANSWER_PART_SPEC_TEXT}"
    )

    text_sections: list[str] = []
    if task_id:
        text_sections.append(f"【task_id】{task_id}")
    meta_line = f"【学段学科】{grade_subject}"
    if q_format:
        meta_line += f" | 【题型】{q_format}"
    text_sections.append(meta_line)
    if anchors_prompt_text.strip():
        text_sections.append(f"【难度校准黄金锚点标尺与词表约束】\n{anchors_prompt_text.strip()}")
    text_sections.append(f"【题干】\n{question_text}")
    if options:
        text_sections.append(f"【选择题选项】{json.dumps(options, ensure_ascii=False)}")

    user_parts: list[dict[str, Any]] = [{"type": "text", "text": "\n\n".join(text_sections)}]
    if images:
        _append_image_blocks(
            user_parts,
            images=images,
            captions=captions,
            embed_images=embed_images,
            assets_dir=assets_dir,
            always_label=not embed_images,
        )

    user_content: Any = user_parts if images else user_parts[0]["text"]
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_content},
    ], bundle


def build_taxonomy_propose_messages(
    *,
    grade_subject: str,
    clusters_summary: list[dict[str, Any]],
    top_pairs: list[dict[str, Any]],
    module_range: tuple[int, int] = (7, 10),
    l2_range: tuple[int, int] = (40, 70),
    config_dir: Path = Path("config"),
    prompt_profile: str | None = None,
) -> list[dict[str, str]]:
    """Stage 3a：基于脱敏语义聚类摘要与高频词表归纳 L1/L2 标准目录树。"""
    bundle = _try_load_seam_bundle(
        grade_subject=grade_subject,
        config_dir=config_dir,
        prompt_profile=prompt_profile,
    )
    stage_seam = _join_seam_sections(bundle, ("stage3_propose",), section="stages")
    seam_tail = f"\n\n【学科归纳补充】\n{stage_seam}" if stage_seam else ""

    system_text = (
        "你是理科课程体系与知识图谱专家。请根据给定学段学科的脱敏知识标签聚类摘要与高频原词榜，"
        f"自底向上归纳一套干净、互斥、层次清晰的二层分类词表（{module_range[0]}~{module_range[1]} 个一级大模块 L1，"
        f"合计 {l2_range[0]}~{l2_range[1]} 个二级知识实体 L2）。\n"
        "严格要求：\n"
        "1. L1 按核心研究对象正交划分，严禁交叉伪大类；L1 内部 L2 名称禁止重复或高度同义，别名收入 `aliases`。\n"
        "2. 仅返回符合 TaxonomyProposeOutput 的合法 JSON 对象。"
        f"{seam_tail}"
    )
    payload = {
        "grade_subject": grade_subject,
        "target_l1_count": list(module_range),
        "target_l2_count": list(l2_range),
        "semantic_clusters": clusters_summary,
        "top_raw_pairs": top_pairs,
    }
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def build_stage3c_mapping_messages(
    *,
    grade_subject: str,
    taxonomy_enum_text: str,
    question_items: list[dict[str, Any]],
    config_dir: Path = Path("config"),
    prompt_profile: str | None = None,
) -> list[dict[str, str]]:
    """Stage 3c：脱敏逐题上下文批映射（禁止传入 question_uid / 来源 / 章节信息）。"""
    bundle = _try_load_seam_bundle(
        grade_subject=grade_subject,
        config_dir=config_dir,
        prompt_profile=prompt_profile,
    )
    stage_seam = _join_seam_sections(bundle, ("stage3c_mapping",), section="stages")
    seam_tail = f"\n\n【词表对齐补充】\n{stage_seam}" if stage_seam else ""

    system_text = (
        "你是理科题库知识词表对齐专家。请结合题目抽象骨架与各步骤原始标签，"
        "将一批脱敏题目中的每一步映射至标准词表中的 `(mapped_l1, mapped_l2)`，并判定整题 `primary_l1_module` 与 `secondary_l1_modules`。\n"
        "严格要求：\n"
        "1. `primary_l1_module`、`secondary_l1_modules` 与每步 `mapped_l1` 必须严格取自给定标准词表的 L1。\n"
        "2. `mapped_l2` 优先向上归入同 L1 下语义最贴近的标准 L2；仅当同 L1 下全部 L2 均无法覆盖时才设 `is_novel_l2=true`。\n"
        "3. 保留每题原始 `task_item_id` 与每步 `step_index`，返回符合 Stage3cBatchMappingOutput 的合法 JSON 对象。"
        f"{seam_tail}"
    )
    user_payload = {
        "grade_subject": grade_subject,
        "taxonomy": taxonomy_enum_text,
        "items": [
            {
                "task_item_id": str(it["task_item_id"]),
                "abstract_skeleton": str(it.get("abstract_skeleton") or ""),
                "raw_primary_l1": str(it.get("raw_primary_l1") or ""),
                "steps": [
                    {
                        "step_index": int(st["step_index"]),
                        "raw_l1": str(st.get("raw_l1") or ""),
                        "raw_l2": str(st.get("raw_l2") or ""),
                        "sub_goal": str(st.get("sub_goal") or ""),
                    }
                    for st in it.get("steps", [])
                ],
            }
            for it in question_items
        ],
    }
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]


def build_stage3d_cluster_assign_messages(
    *,
    grade_subject: str,
    l1_module: str,
    l2_entity: str,
    raw_l3_items: list[dict[str, Any]],
    taxonomy_version: str = "",
    target_canonical_range: tuple[int, int] = (5, 15),
    pre_clusters: list[dict[str, Any]] | None = None,
    config_dir: Path = Path("config"),
    prompt_profile: str | None = None,
) -> list[dict[str, str]]:
    """Stage 3d（D-Audit-2）：同 (grade_subject, taxonomy_version, L1, L2) 域 Cluster-then-Assign 标准算子归纳。"""
    bundle = _try_load_seam_bundle(
        grade_subject=grade_subject,
        config_dir=config_dir,
        prompt_profile=prompt_profile,
    )
    stage_seam = _join_seam_sections(bundle, ("stage3d_cluster_assign",), section="stages")
    seam_tail = f"\n\n【算子归纳补充规范】\n{stage_seam}" if stage_seam else ""

    lo, hi = int(target_canonical_range[0]), int(target_canonical_range[1])
    system_text = (
        "你是理科知识图谱原子算子归纳专家（Stage 3d Cluster-then-Assign）。"
        f"给定严格限定在「{grade_subject} / {l1_module} / {l2_entity}」作用域内的全部去重词条及频次，"
        f"请归纳提炼出 {lo}~{hi} 个标准 `canonical_name`，并将每一条输入词条精确映射归入其中一个标准项。\n"
        "严格要求：\n"
        "1. 严格遵循下方【算子归纳补充规范】的三态作用域命名协议（常规域 4~14 字脱壳算子 / MOTIF 公认母题正名 / METHOD 4~6 字技法名）及宁独不混原则。\n"
        "2. 严禁创建“其他/杂项”垃圾桶簇；输入清单中的所有词条必须 100% 覆盖分配，不得遗漏。\n"
        "3. 仅返回合法 JSON 对象：`{\"canonical_operators\": [{\"canonical_name\": \"...\", \"rationale\": \"...\", \"members\": [\"...\"]}], \"assignments\": {\"原raw_l3\": \"canonical_name\"}}`。"
        f"{seam_tail}"
    )

    sanitized_items: list[dict[str, Any]] = []
    for it in raw_l3_items:
        op_name = str(it.get("raw_l3") or it.get("l3_operator") or it.get("name") or "")
        if not op_name.strip():
            continue
        raw_freq = it.get("freq")
        if raw_freq is None:
            raw_freq = it.get("count", 1)
        item_entry: dict[str, Any] = {
            "raw_l3": op_name,
            "freq": int(raw_freq) if raw_freq is not None else 1,
        }
        if it.get("node_id"):
            item_entry["node_id"] = str(it["node_id"])
        if it.get("cluster_id") is not None:
            item_entry["cluster_id"] = int(it["cluster_id"])
        sanitized_items.append(item_entry)
    user_payload: dict[str, Any] = {
        "grade_subject": grade_subject,
        "taxonomy_version": taxonomy_version,
        "scope": {"l1_module": l1_module, "l2_entity": l2_entity},
        "target_canonical_count": [lo, hi],
        "raw_l3_items": sanitized_items,
    }
    if pre_clusters:
        user_payload["pre_clusters"] = [
            {
                "cluster_id": int(c.get("cluster_id", idx)),
                "members": [str(m) for m in (c.get("members") or [])],
            }
            for idx, c in enumerate(pre_clusters)
        ]

    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]


def build_merge_judge_messages(
    *,
    target_kind: str,
    scope_desc: str,
    candidate_pairs: list[dict[str, Any]],
    grade_subject: str = "小学奥数",
    config_dir: Path = Path("config"),
    prompt_profile: str | None = None,
) -> list[dict[str, str]]:
    """Stage 3d：L3 节点或全局概念候选对同义归并批裁判（零 question_uid）。"""
    bundle = _try_load_seam_bundle(
        grade_subject=grade_subject,
        config_dir=config_dir,
        prompt_profile=prompt_profile,
    )
    stage_seam = _join_seam_sections(bundle, ("stage3d_cluster_assign",), section="stages")
    seam_tail = f"\n\n【归并补充准则】\n{stage_seam}" if stage_seam else ""

    system_text = (
        "你是理科知识点与解题概念去重裁判。给定同一严格作用域内的候选同义词对，"
        "请判断每一对是否本质上指代同一知识点/方法/母题。\n"
        "严格要求：\n"
        "1. 仅当两者数学含义等价、仅为表述/语序/简繁差异时设 `should_merge=true`，并按下方【归并补充准则】给出规范 `canonical_name`；存在核心条件或模型差异必须设 `should_merge=false`。\n"
        "2. 返回符合 MergeJudgeBatchOutput 的合法 JSON 对象。"
        f"{seam_tail}"
    )
    user_payload = {
        "target_kind": target_kind,
        "scope": scope_desc,
        "pairs": [
            {
                "pair_id": str(p["pair_id"]),
                "name_a": str(p["name_a"]),
                "name_b": str(p["name_b"]),
                "cosine_sim": round(float(p.get("cosine_sim", 0.0)), 4),
                "jaccard": round(float(p.get("jaccard", 0.0)), 4),
            }
            for p in candidate_pairs
        ],
    }
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]


def build_prerequisite_dag_messages(
    *,
    grade_subject: str,
    taxonomy_enum_text: str,
    ordered_transitions: list[dict[str, Any]],
    config_dir: Path = Path("config"),
    prompt_profile: str | None = None,
) -> list[dict[str, str]]:
    """Stage 3e：基于步序共现转移矩阵构建 L2 先修有向无环图（DAG）。"""
    bundle = _try_load_seam_bundle(
        grade_subject=grade_subject,
        config_dir=config_dir,
        prompt_profile=prompt_profile,
    )
    stage_seam = _join_seam_sections(bundle, ("stage3e_prerequisite",), section="stages")
    seam_tail = f"\n\n【先修图谱补充】\n{stage_seam}" if stage_seam else ""

    system_text = (
        "你是理科课程先修依赖分析师。给定解题步骤中观察到的转移频次与标准词表，"
        "请提炼真正具有教学先修关系（掌握 source 才能顺利学习/应用 target）的 L2 有向边列表。\n"
        "严格要求：\n"
        "1. `source_l1/source_l2` 与 `target_l1/target_l2` 必须严格取自标准词表。\n"
        "2. 严禁自环与有向环（DAG），严禁步序倒置伪边与偶然共现边。\n"
        "3. 返回符合 PrerequisiteDagOutput 的合法 JSON 对象。"
        f"{seam_tail}"
    )
    user_payload = {
        "grade_subject": grade_subject,
        "taxonomy": taxonomy_enum_text,
        "observed_step_transitions": ordered_transitions,
    }
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]


def build_stage4_refine_messages(
    *,
    candidate_pairs: list[dict[str, Any]],
    grade_subject: str = "小学奥数",
    config_dir: Path = Path("config"),
    prompt_profile: str | None = None,
) -> list[dict[str, str]]:
    """Stage 4：针对 CONTRASTIVE 及边界带 ISOMORPHIC/SCAFFOLD 的脱敏精裁（零 question_uid，D-Audit-5 锁死 SCAFFOLD 方向）。"""
    bundle = _try_load_seam_bundle(
        grade_subject=grade_subject,
        config_dir=config_dir,
        prompt_profile=prompt_profile,
    )
    stage_seam = _join_seam_sections(bundle, ("stage4_refine",), section="stages")
    seam_tail = f"\n\n【精裁补充规则】\n{stage_seam}" if stage_seam else ""

    system_text = (
        "你是理科题库教研拓扑质检员。请审核以下候选题目关系对（已脱敏，不含题号与来源）。\n"
        "硬性方向与输出契约：\n"
        "- `SCAFFOLD`（D-Audit-5 方向锁死）：`source = 降阶铺垫子题（低 Elo） → target = 升维母题（高 Elo）`，必须满足真子步包含。\n"
        "- 为每对给出 `is_valid`（true/false）及考后复盘 `pedagogical_note`（无效置 `\"\"`，有效严格按下方【精裁补充规则】对应句式书写），返回 Stage4RefineBatchOutput JSON。"
        f"{seam_tail}"
    )
    sanitized_pairs = [
        {
            "pair_id": str(p["pair_id"]),
            "relation_type": str(p["relation_type"]),
            "source_text": str(p.get("source_text") or "")[:500],
            "source_skeleton": str(p.get("source_skeleton") or "")[:300],
            "source_tags": list(p.get("source_tags") or []),
            "target_text": str(p.get("target_text") or "")[:500],
            "target_skeleton": str(p.get("target_skeleton") or "")[:300],
            "target_tags": list(p.get("target_tags") or []),
            "metrics": {
                "surface_pct": round(float(p.get("surface_pct", 0.0)), 3),
                "skeleton_pct": round(float(p.get("skeleton_pct", 0.0)), 3),
                "k_jaccard": round(float(p.get("k_jaccard", 0.0)), 3),
                "containment": round(float(p.get("containment", 0.0)), 3),
            },
        }
        for p in candidate_pairs
    ]
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": json.dumps({"pairs": sanitized_pairs}, ensure_ascii=False)},
    ]


def build_external_diagnose_messages(
    *,
    grade_subject: str,
    question_text: str,
    taxonomy_enum_text: str,
    preprocessed_image_paths: list[Path] | None = None,
    anchors_prompt_text: str = "",
    config_dir: Path = Path("config"),
    prompt_profile: str | None = None,
) -> list[dict[str, Any]]:
    """外部新错题实时单路诊断消息构造（严格字段白名单，EXIF 矫正图片入 VLM，含 D4 标尺与黄金锚点）。"""
    bundle = _try_load_seam_bundle(
        grade_subject=grade_subject,
        config_dir=config_dir,
        prompt_profile=prompt_profile,
    )
    seam_notes = _join_seam_sections(
        bundle,
        (
            "persona",
            "visual_extraction",
            "solving_conventions",
            "steps_and_socratic_hint",
            "abstraction_and_tagging",
            "difficulty_rubric_notes",
        ),
    )
    seam_section = f"\n\n【学科与接缝补充规范】\n{seam_notes}" if seam_notes else ""

    system_text = (
        f"你是严谨的{grade_subject}学科错题诊断专家。请阅读题干与配图，严格输出一个合法 JSON 对象（不要输出 Markdown 代码块包裹），并遵守以下全部硬性契约（教研细则见下方【学科与接缝补充规范】）：\n"
        "1. `visual_facts` 必须排在 JSON 顶层首位（无图题置空字符串 \"\"），仅写客观视觉事实，严禁写入未画出的辅助线或推导结论；"
        "遇到正多边形、星形或网格配图时，必须先用对称性或投影严格核对内部对角线/跨段虚线与外周短边的真实倍数关系（例如正六角星相对凹顶点连线等于中间正六边形主对角线即 2 倍短边，切勿凭直觉误判为 1 倍）。\n"
        "2. 必须输出非空的 `method_summary`（≥2字）与 `solution_markdown`（≥5字），严禁留空。\n"
        "3. `sympy_code`（存在 `verify_mode == \"SYMBOLIC\"` 小问时必填，否则置 `null`）：仅允许导入 sympy / math / fractions；"
        "严禁调用 sympify / parse_expr / lambdify / S(<字符串>) / eval / exec / open；"
        "**必须定义 `def verify():`** 返回字典（键为归一后小问标签如 `'主问'` 或 `'(1)'`，值为通过真实运算得出的字符串或列表，禁止仅用 print(...) 或硬编码常量）。\n"
        "4. `steps`（1~5 个有教学意义的逻辑推理步，`step_index` 从 1 连续递增）：\n"
        "   - 必填 `sub_goal`、`core_formula`、`hint`（设问式零泄密，严禁剧透终值）；\n"
        "   - **严禁凑步膨胀**：单一基础公式直推题（如基础追及超车 `(L1+L2)÷(v1-v2)`、基本 `s=vt` 三量换算）必须控制在 **1~2 个核心逻辑步**，严禁把求差、求和、代入除法拆成 3~4 个微步骤；\n"
        "   - `raw_l1`（**必须取自下方标准词表的 L1**）、`raw_l2`（**优先取自下方标准词表的 L2**）、`raw_l3`（**4~14 字脱壳原子转化算子短语**，严禁数字/人名道具/整句解说）。\n"
        "5. `abstract_skeleton`（三段式去情境抽象骨架）、`primary_l1_module`、`secondary_l1_modules`、`matched_l2` 严格从下方标准词表选取；若题目属于跨模块融合（如几何多边形周长×行程、道路多车行程×极端原理最值构造、扶梯动态存量×牛吃草），**必须同时在 `secondary_l1_modules` 与 `matched_l2` 中完整列出载体模块与方法模块**，严禁单边归入杂题而丢失领域标签；`thinking_methods`（1~3 个具体技法，禁用万金油泛词）、`classic_motifs`（未命中置 `[]`）。\n"
        f"6. {_TRAP_ENUM_SPEC_TEXT}\n"
        f"7. {_DIFFICULTY_RUBRIC_SPEC_TEXT}\n"
        "   - **反 `[2,2,2,1]` 均值坍缩硬红线**：仅纯静态单公式一步直推题打 `C=1, S=1, T=1`（L1_基础直达），动态扶梯双状态级数守恒消元、多次往返调和比例等两步转化题应客观评为 `L2~L3`（`C=2`）；"
        "凡涉及**多体同步节点联立消元、变动起点双比例消元、错时往返截取对称段、或需反证判定折返区间（直觉极易误判首末段状态）**的题目，必须客观评定 `C=3`（强反直觉陷阱同步评 `T=3`），严禁一律折中塞入 `C=2, S=2`！"
        f"{seam_section}\n\n"
        "【输出 JSON 顶层结构模板（注意：difficulty 中各维 score 仅为合法整数类型示意，请严格按实际复杂度在允许区间内拉开的档位打分，严禁照抄示意数字）】\n"
        "{\n"
        '  "visual_facts": "",\n'
        '  "visual_ambiguity_flag": false,\n'
        '  "visual_ambiguity_note": "",\n'
        '  "method_summary": "一句话解法路线",\n'
        '  "solution_markdown": "完整推导过程（Markdown/LaTeX）",\n'
        '  "final_answers": [ ... ],\n'
        '  "sympy_code": "def verify():\\n    ...\\n    return {\'主问\': str(ans)}\\n",\n'
        '  "steps": [{"step_index": 1, "sub_goal": "...", "core_formula": "...", "hint": "...", "raw_l1": "标准L1", "raw_l2": "标准L2", "raw_l3": "4~14字原子算子"}],\n'
        '  "abstract_skeleton": "去情境抽象骨架（无具体数字）",\n'
        '  "primary_l1_module": "标准L1",\n'
        '  "secondary_l1_modules": [],\n'
        '  "matched_l2": ["标准L2"],\n'
        '  "thinking_methods": ["具体技法"],\n'
        '  "classic_motifs": ["经典母题"],\n'
        '  "trap_tags": ["TRAP_FRAME_SHIFT"],\n'
        '  "difficulty": {\n'
        '    "C_concept_count": {"evidence": "点明核心模型层级（基础单模型打1，常规串联打2，深层不变量/反证/多体消元打3，非标构造打4）", "score": 1},\n'
        '    "S_chain_length": {"evidence": "点明合并纯算术后的真实逻辑步数或独立分类分支数（1~2步打1，3~4步打2，≥5步或多分支打3）", "score": 1},\n'
        '    "T_trickiness": {"evidence": "点明条件是否直白（直白打1，含1处隐含转译打2，强反直觉/极易漏解陷阱打3）", "score": 1},\n'
        '    "L_computation_load": {"evidence": "点明运算负荷（短题心算打1，常规列式消元打2，繁算多联立打3）", "score": 1}\n'
        "  }\n"
        "}\n\n"
        "【`final_answers` 元素 `AnswerPart` 规范】\n"
        f"{_ANSWER_PART_SPEC_TEXT}"
    )
    text_sections = [
        f"【学段学科】{grade_subject}",
        f"【当前标准词表】\n{taxonomy_enum_text}",
    ]
    if anchors_prompt_text.strip():
        text_sections.append(f"【难度校准黄金锚点与词表约束】\n{anchors_prompt_text.strip()}")
    text_sections.append(f"【题干】\n{question_text}")

    user_parts: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": "\n\n".join(text_sections),
        }
    ]
    imgs = preprocessed_image_paths or []
    multi = len(imgs) >= 2
    for idx, ipath in enumerate(imgs, start=1):
        if multi:
            user_parts.append({"type": "text", "text": f"【配图 {idx}】"})
        user_parts.append(
            {"type": "image_url", "image_url": {"url": _load_transposed_image_data_url(ipath)}}
        )
    user_content: Any = user_parts if imgs else user_parts[0]["text"]
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_content},
    ]


def build_diagnose_rerank_messages(
    *,
    grade_subject: str,
    mistake_summary: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """构造外部错题在线推荐候选池（Top-12）轻量纯文本精裁消息（严格脱敏白名单，零 uid/source_id/chapter）。"""
    system_text = (
        f"你是资深{grade_subject}教研推荐审校专家。学生刚做错一道原题，系统已从题库初筛出一批脱敏候选题。\n"
        "请逐一严格审查每道候选题与原错题的**底层数学子模型、核心转化算子及易错陷阱**，将其精准归入以下四类之一（宁缺毋滥，不达标一律判 `\"REJECT\"`）：\n"
        "1. `\"SCAFFOLD\"`（推荐前置铺垫题）：必须精准拆解原题的某一个核心子技能/前置步骤（如跨模块几何边长拆份、和差倍份数、或**错时往返问题中截取‘首遇到折返同时抵端’的同步等时对称段定速度/路程比**），且认知负荷严格低于原题。\n"
        "**严禁放行**：① 比例机制或符号方向相反（如原题是等速异时正比差倍或**路程比×速度比同时缩放求单程耗时**，严禁放行**定路程往返总时长按速度反比拆分题**；**原题是自动扶梯顺行总级数加号守恒且人速可变，严禁放行流水行船题或减号抵消的牛吃草问题**）；② 子模型或条件形态错位（如原题有具体米数距离，严禁放行设单位1无具体量的工程类比题；原题为纯静态构筑物过桥隧，严禁放行含运动行人迎面/追及速度和差题；**原题各时刻头尾状态唯一确定无多解，严禁机械放行“相遇/追及前后相距d米”双解分类讨论题**）；③ 已被同批另一道更高贴合度铺垫题完全覆盖全部子技能的重复凑数题。\n"
        "2. `\"ISOMORPHIC\"`（推荐同构变式题）：必须与原题共享**一致的底层核心物理/数学子模型与等量关系骨架**，更换场景载体、数值、设问方向或合理递进。\n"
        "**准入与禁令边界**：允许跨载体的真定距反比同构迁移（例如陆地满载/空车往返按速度反比求单程 ↔ 轮船顺流/逆流往返按时间反比与速差求单程）；**严禁放行**：① 仅共享二元一次消元代数外壳但物理参考系相反的跨模型题（**严禁将自动扶梯级数守恒题与火车变速过桥隧题互判为同构**）；② **退化丢核题**（如原题核心是双车车身长度之和超车，严禁放行无车长的纯质点追及题；原题是完整往返求距离应用题，严禁放行无具体量的纯百分数增减概念小题）；③ **扶梯内部条件翻转题**（若原题为“人变速、显式给时间、同向顺行”，而候选为“人匀速、未给时间靠步数比定时间比、一顺一逆上下扶梯”，**严禁放入 `ISOMORPHIC`，必须改判入 `CONTRASTIVE_TRAP`**）；④ 同时出发 3S 倍率题塞进错时出发变式栏，或相向错车塞进同向超车变式栏。\n"
        "3. `\"CONTRASTIVE_TRAP\"`（推荐辨析/同坑排雷题）：必须**精准命中原题的真实核心易错坑**，或与原题**表面形似但因一处关键条件翻转导致列式分叉**（如：折返后距 **A 地** `2S-s2` vs 距 **B 地** `S+s2`；**错时提前出发破坏首段同步性严禁套 3S** vs 同时出发可套 3S 倍率［注意：**严禁把“错时不可乘3”错误归因为“两遇点重合”**，同时等速重合于中点仍满足3倍关系］；**自动扶梯人变速给时间两次同向顺加 vs 人匀速不给时间顺逆异向用步数比定时间比**；**等路程往返调和平均速度 vs 一半时间一半路程算术平均速度比较**；过临界点前后双解 vs 慢者追不上单解；误把跨折返点首小时当纯顺水；整列火车在桥上 `L桥-L车` vs 完全过桥 `L桥+L车`；地面看两车长和 vs 车上窗前看单车长；打破十字路口中心会合定式的瓶颈极值 $\\min\\max t_i$ vs 总耗时最少 $\\min\\sum t_i$）。\n"
        "**严禁放行**：具体运动机制脱节的伪对比（如**原题是三人同向同速无相遇算剩余距离，严禁配对两人相向异速围绕中点相遇题**；原题是错时同点往返，严禁配对同时出发中点左右双解增根题；或仅共享宽泛陷阱标签但设错机制无关的跨场景难题）。\n"
        "4. `\"REJECT\"`：不符合上述三类严苛教研标准的一切候选题（宁可某栏目返回 0~2 题，绝不拿弱相关题凑数）。\n\n"
        "输出要求：\n"
        "- 允许纠正栏目：若某候选的 `proposed_slot` 不够贴切，但完美符合另一栏目的标准且满足难度关系，可直接将其 `verdict` 改判为更合适的栏目。\n"
        "- `decisions` 必须覆盖输入的全部 `candidate_id`；当 `verdict != \"REJECT\"` 时，`pedagogical_reason` 用 15~45 字点明具体铺垫环节/同构核/排雷辨析点（**严禁泄露候选题答案数值**）；`verdict == \"REJECT\"` 时 `pedagogical_reason` 置 `\"\"`。\n"
        "- 仅返回合法 JSON：`{\"decisions\": [{\"candidate_id\": \"c01\", \"verdict\": \"SCAFFOLD\"|\"ISOMORPHIC\"|\"CONTRASTIVE_TRAP\"|\"REJECT\", \"pedagogical_reason\": \"...\"}]}`。"
    )

    clean_mistake = {
        "question_text": str(mistake_summary.get("question_text") or "")[:600],
        "abstract_skeleton": str(mistake_summary.get("abstract_skeleton") or "")[:300],
        "primary_l1_module": str(mistake_summary.get("primary_l1_module") or ""),
        "matched_l2": list(mistake_summary.get("matched_l2") or []),
        "step_operators": list(mistake_summary.get("step_operators") or []),
        "thinking_methods": list(mistake_summary.get("thinking_methods") or []),
        "classic_motifs": list(mistake_summary.get("classic_motifs") or []),
        "trap_tags": list(mistake_summary.get("trap_tags") or []),
        "ref_elo": int(mistake_summary.get("ref_elo") or 1500),
        "ref_tier": str(mistake_summary.get("ref_tier") or ""),
    }
    clean_candidates = [
        {
            "candidate_id": str(c["candidate_id"]),
            "proposed_slot": str(c.get("proposed_slot") or ""),
            "base_elo": int(c.get("base_elo") or 1500),
            "tier": str(c.get("tier") or ""),
            "primary_l1_module": str(c.get("primary_l1_module") or ""),
            "l2_entities": list(c.get("l2_entities") or []),
            "l3_operators": list(c.get("l3_operators") or []),
            "thinking_methods": list(c.get("thinking_methods") or []),
            "trap_tags": list(c.get("trap_tags") or []),
            "surface_sim": round(float(c.get("surface_sim", 0.0)), 3),
            "skeleton_sim": round(float(c.get("skeleton_sim", 0.0)), 3),
            "question_text": str(c.get("question_text") or "")[:450],
            "abstract_skeleton": str(c.get("abstract_skeleton") or "")[:260],
        }
        for c in candidates
    ]
    user_payload = {
        "grade_subject": grade_subject,
        "mistake": clean_mistake,
        "candidates": clean_candidates,
    }
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]


def render_prompt_preview(
    *,
    stage: str,
    grade_subject: str = "小学奥数",
    config_dir: Path = Path("config"),
    prompt_profile: str | None = None,
    sample_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按 stage 渲染完整 Prompt 预览与继承溯源元信息（供 `shuati prompt render` 使用）。"""
    bundle = load_prompt_seams(
        config_dir=config_dir,
        grade_subject=grade_subject,
        profile=prompt_profile,
    )
    norm_stage = (stage or "").strip().lower()
    payload = dict(sample_payload or {})
    messages: list[dict[str, Any]]
    version_key: str
    schema_cls: type[BaseModel] | None = None

    if norm_stage in ("1", "stage1", "s1", "stage1_primary", "stage1_secondary"):
        role: Literal["PRIMARY", "SECONDARY"] = "SECONDARY" if norm_stage == "stage1_secondary" else "PRIMARY"
        raw_opts = payload.get("options")
        stage1_opts: dict[str, str] = (
            {str(k): str(v) for k, v in raw_opts.items()} if isinstance(raw_opts, dict) else {}
        )
        inp = Stage1Input(
            question_uid="__preview_sanitized__",
            grade_subject=grade_subject,
            question_text=str(payload.get("question_text") or "甲乙两地相距 120 千米，货车往返需 5 小时，求平均速度。"),
            cas_images=[],
            image_captions=[],
            options=stage1_opts,
            has_images=False,
        )
        messages = build_stage1_messages(
            inp,
            role=role,
            assets_dir=Path("data/assets/images"),
            config_dir=config_dir,
            prompt_profile=prompt_profile,
        )
        version_key = "stage1"
        schema_cls = Stage1SecondaryOutput if role == "SECONDARY" else Stage1PrimaryOutput
    elif norm_stage in ("stage1_judge", "judge_qualitative", "qualitative_judge"):
        messages = build_qualitative_judge_messages(
            label=str(payload.get("label") or "(2)"),
            answer_a=str(payload.get("answer_a") or "连接正方形对角线分割面积"),
            answer_b=str(payload.get("answer_b") or "过顶点作对角线平分图形"),
            question_text=str(payload.get("question_text") or "请说明分割正方形面积的作法。"),
            grade_subject=grade_subject,
            config_dir=config_dir,
            prompt_profile=prompt_profile,
        )
        version_key = "stage1"
        schema_cls = QualitativeJudgeOutput
    elif norm_stage in ("judge_multi", "multi_solution_judge"):
        messages = build_multi_solution_judge_messages(
            summary_a=str(payload.get("summary_a") or "等高模型面积比例法"),
            sol_a=str(payload.get("sol_a") or "利用共高三角形底边比等于面积比求解。"),
            summary_b=str(payload.get("summary_b") or "设未知数列一次方程求解"),
            sol_b=str(payload.get("sol_b") or "设阴影面积为 x，依总面积列方程求解。"),
            grade_subject=grade_subject,
            config_dir=config_dir,
            prompt_profile=prompt_profile,
        )
        version_key = "stage1"
        schema_cls = MultiSolutionJudgeOutput
    elif norm_stage in ("2", "stage2", "s2", "stage2_only"):
        messages = build_stage2_messages(
            grade_subject=grade_subject,
            question_text=str(payload.get("question_text") or "如图，正方形 ABCD 边长为 4，求阴影部分面积。"),
            visual_facts=str(payload.get("visual_facts") or "正方形 ABCD 中连接对角线 AC，阴影为三角形 ABC。"),
            verified_final_answers=payload.get("verified_final_answers")
            or [{"label": "主问", "answer_type": "NUMERIC", "verify_mode": "SYMBOLIC", "collection_mode": "SINGLE", "answer": "8", "sympy_expr": "8"}],
            verified_method_summary=str(payload.get("verified_method_summary") or "正方形面积取半"),
            verified_solution_markdown=str(payload.get("verified_solution_markdown") or "阴影面积为正方形面积的一半，即 4×4÷2 = 8。"),
            anchors_prompt_text=str(payload.get("anchors_prompt_text") or ""),
            has_images=bool(payload.get("has_images", False)),
            has_human_visual_override=bool(payload.get("has_human_visual_override", False)),
            has_human_answer_override=bool(payload.get("has_human_answer_override", False)),
            task_id=str(payload.get("task_id") or "T0001"),
            config_dir=config_dir,
            prompt_profile=prompt_profile,
        )
        version_key = "stage2"
        schema_cls = Stage2OnlyOutput
    elif norm_stage in ("unified", "unified_s1_s2"):
        task_payload: dict[str, Any] = {
            "task_id": str(payload.get("task_id") or "T0001"),
            "mode": str(payload.get("mode") or "UNIFIED_S1_S2"),
            "grade_subject": grade_subject,
            "allow_pi_314": bool(payload.get("allow_pi_314", grade_subject == "小学奥数")),
            "question_text": str(payload.get("question_text") or "一个圆半径为 3 厘米，求面积（π 取 3.14）。"),
            "options": payload.get("options") if isinstance(payload.get("options"), dict) else {},
            "has_images": bool(payload.get("has_images", False)),
            "image_paths": list(payload.get("image_paths") or []),
            "image_captions": list(payload.get("image_captions") or []),
        }
        messages, bundle = build_unified_s1_s2_messages(
            task_payload=task_payload,
            anchors_prompt_text=str(payload.get("anchors_prompt_text") or ""),
            config_dir=config_dir,
            prompt_profile=prompt_profile,
            embed_images=False,
        )
        version_key = "unified"
        schema_cls = UnifiedS1S2Output
    elif norm_stage in ("diagnose", "external_diagnose"):
        messages = build_external_diagnose_messages(
            grade_subject=grade_subject,
            question_text=str(payload.get("question_text") or "甲乙两人同时从相距 100 米的两地相向而行。"),
            taxonomy_enum_text=str(payload.get("taxonomy_enum_text") or "- 行程问题: 相遇与追及"),
            anchors_prompt_text=str(payload.get("anchors_prompt_text") or ""),
            config_dir=config_dir,
            prompt_profile=prompt_profile,
        )
        version_key = "diagnose"
        schema_cls = ExternalDiagnosisOutput
    elif norm_stage in ("3a", "stage3a", "propose", "stage3_propose"):
        messages = build_taxonomy_propose_messages(
            grade_subject=grade_subject,
            clusters_summary=list(payload.get("clusters_summary") or [{"cluster_id": 0, "total_freq": 10, "representative": "行程问题 / 相遇问题"}]),
            top_pairs=list(payload.get("top_pairs") or [{"raw_l1": "行程问题", "raw_l2": "相遇问题", "cnt": 10}]),
            config_dir=config_dir,
            prompt_profile=prompt_profile,
        )
        version_key = "stage3a"
        schema_cls = TaxonomyProposeOutput
    elif norm_stage in ("3c", "stage3c", "stage3c_mapping"):
        messages = build_stage3c_mapping_messages(
            grade_subject=grade_subject,
            taxonomy_enum_text=str(payload.get("taxonomy_enum_text") or "- 行程问题: 相遇与追及"),
            question_items=list(payload.get("question_items") or [{"task_item_id": "T0001", "abstract_skeleton": "相向运动求相遇时间", "steps": [{"step_index": 1, "raw_l1": "行程", "raw_l2": "相遇", "sub_goal": "求速度和"}]}]),
            config_dir=config_dir,
            prompt_profile=prompt_profile,
        )
        version_key = "stage3c"
        schema_cls = Stage3cBatchMappingOutput
    elif norm_stage in ("3d", "stage3d", "stage3d_cluster_assign"):
        messages = build_stage3d_cluster_assign_messages(
            grade_subject=grade_subject,
            l1_module=str(payload.get("l1_module") or "平面几何"),
            l2_entity=str(payload.get("l2_entity") or "等高模型"),
            raw_l3_items=list(payload.get("raw_l3_items") or [{"raw_l3": "等高三角形面积比转化", "freq": 5}, {"raw_l3": "同高三角形底边比例转化", "freq": 3}]),
            config_dir=config_dir,
            prompt_profile=prompt_profile,
        )
        version_key = "stage3d"
        schema_cls = Stage3dClusterAssignOutput
    elif norm_stage in ("merge_judge",):
        messages = build_merge_judge_messages(
            target_kind=str(payload.get("target_kind") or "KNOWLEDGE_NODE"),
            scope_desc=str(payload.get("scope_desc") or f"{grade_subject}/平面几何/等高模型"),
            candidate_pairs=list(payload.get("candidate_pairs") or [{"pair_id": "p1", "name_a": "等高面积比", "name_b": "同高面积比", "cosine_sim": 0.92, "jaccard": 0.6}]),
            grade_subject=grade_subject,
            config_dir=config_dir,
            prompt_profile=prompt_profile,
        )
        version_key = "stage3d"
        schema_cls = MergeJudgeBatchOutput
    elif norm_stage in ("3e", "stage3e", "stage3e_prerequisite"):
        messages = build_prerequisite_dag_messages(
            grade_subject=grade_subject,
            taxonomy_enum_text=str(payload.get("taxonomy_enum_text") or "- 平面几何: 等高模型, 蝴蝶模型"),
            ordered_transitions=list(payload.get("ordered_transitions") or [{"from_l1": "平面几何", "from_l2": "等高模型", "to_l1": "平面几何", "to_l2": "蝴蝶模型", "cnt": 8}]),
            config_dir=config_dir,
            prompt_profile=prompt_profile,
        )
        version_key = "stage3e"
        schema_cls = PrerequisiteDagOutput
    elif norm_stage in ("4", "stage4", "stage4_refine"):
        messages = build_stage4_refine_messages(
            candidate_pairs=list(payload.get("candidate_pairs") or [{"pair_id": "p1", "relation_type": "SCAFFOLD", "source_text": "基础铺垫题", "target_text": "综合母题"}]),
            grade_subject=grade_subject,
            config_dir=config_dir,
            prompt_profile=prompt_profile,
        )
        version_key = "stage4"
        schema_cls = Stage4RefineBatchOutput
    else:
        raise ValueError(f"未知的 prompt stage: {stage!r}")

    result: dict[str, Any] = {
        "stage": norm_stage,
        "grade_subject": bundle.grade_subject,
        "slug": bundle.slug,
        "profile": bundle.profile,
        "chain": list(bundle.chain),
        "prompt_version": bundle.prompt_versions.get(version_key, ""),
        "prompt_digest": bundle.prompt_digest,
        "prompt_versions": dict(bundle.prompt_versions),
        "sources": {k: list(v) for k, v in bundle.sources.items()},
        "messages": messages,
    }
    if schema_cls is not None:
        result["flattened_schema"] = flatten_json_schema(schema_cls)
    return result
