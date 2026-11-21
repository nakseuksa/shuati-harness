"""字段白名单 Prompt 构造器：严禁 uid / source_collection / chapter / 原始图片路径入 Prompt，内存 EXIF 矫正与多图显式编号。"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any, Literal

from PIL import Image, ImageOps

from shuati.models.schemas import Stage1Input

STAGE1_PROMPT_VERSION = "s1_v1"

_MIME_BY_FORMAT: dict[str, str] = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
    "BMP": "image/bmp",
}


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


def build_stage1_messages(
    inp: Stage1Input,
    *,
    role: Literal["PRIMARY", "SECONDARY"],
    assets_dir: Path,
) -> list[dict[str, Any]]:
    """严格字段白名单组装 Stage 1 消息。

    - 严禁将 inp.question_uid、source_collection、chapter、ch\\d+_、legacy_tags、原始图片路径写入任何 message；
    - 有图时从 assets_dir / cas_img 读取，内存执行 ImageOps.exif_transpose()，转 base64 data URL；
    - len(cas_images) >= 2 时，每张图前插入文本块：【配图 {i}】或【配图 {i}（{caption}）】；
    - 明确声明：禁止自造 (1)-k 子小问标签；未指定 π 近似值时保留精确符号 pi；visual_facts 严禁写入未画出的辅助线或推导结论。
    """
    role_desc = (
        "你是主路独立解题与验真专家（PRIMARY），需在解答末尾给出可独立运行的 Python SymPy 验算脚本 sympy_code（定义 def verify(): 返回 dict[str, str | list[str]]）。"
        if role == "PRIMARY"
        else "你是辅路双盲独立解题专家（SECONDARY），请完全独立读题看图并解答，严禁猜测他人解法。"
    )

    sympy_rule = (
        "\n7. sympy_code 规范（存在 SYMBOLIC 小问时必填）："
        "仅允许导入 sympy / math / fractions；严禁调用 sympify / parse_expr / lambdify / S(<字符串>) / eval / exec / open；"
        "必须定义 def verify(): 返回字典，键为归一后的小问标签（如 '主问' 或 '(1)'），值必须经由真实数学运算得出（禁止硬编码常量返回）。"
        if role == "PRIMARY"
        else ""
    )

    system_text = (
        f"你是一位严谨的{inp.grade_subject}学科解题专家。{role_desc}\n"
        "请严格输出一个合法的 JSON 对象，并遵守以下硬性规则：\n"
        "1. JSON 字段顺序中 visual_facts 必须排在首位！若存在配图，visual_facts 须客观描述图中全部可见元素"
        "（顶点字母、线面位置关系、垂直/平行/等长/角度标注、阴影区域封闭边界与包含关系）；"
        "visual_facts 严禁写入原图未画出的辅助线、未标注的几何性质或任何推导结论。无图题 visual_facts 置空字符串 \"\"。\n"
        "2. 若图片模糊、标注缺失或阴影边界有歧义，必须将 visual_ambiguity_flag 设为 true 并在 visual_ambiguity_note 说明。\n"
        "3. 小问标签归一规范：题干有显式编号时统一使用 \"(1)\", \"(2)\"...；单问使用 \"主问\"；"
        "无编号“计算+说理”复合问拆为 \"(1)\" 与 \"(2)\"；严禁自造 \"(1)-1\"、\"(1)-k\" 等子小问标签！\n"
        "4. 同一小问多空或多值使用 collection_mode（SINGLE / ORDERED_TUPLE / UNORDERED_MULTISET / LABELED_MAP）装入 items。\n"
        "5. answer_type 与 verify_mode 必须严格匹配："
        "NUMERIC/EXPRESSION/RATIO/SET_OR_INTERVAL -> SYMBOLIC；CHOICE/SYMBOL_FILL -> EXACT；TEXT/PROOF -> QUALITATIVE。\n"
        "6. 数值约定：题干未明确指定 π 近似值（如“π 取 3.14”）时，sympy_expr 必须保留精确符号 pi、sqrt(...) 或分数。"
        f"{sympy_rule}"
    )

    user_parts: list[dict[str, Any]] = []
    text_lines = [f"【学段学科】{inp.grade_subject}", f"【题干】\n{inp.question_text}"]
    if inp.options:
        opts_json = json.dumps(inp.options, ensure_ascii=False)
        text_lines.append(f"【选择题选项】{opts_json}")
    user_parts.append({"type": "text", "text": "\n\n".join(text_lines)})

    images = inp.cas_images or []
    captions = inp.image_captions or []
    multi = len(images) >= 2
    for idx, cas_img in enumerate(images, start=1):
        cap = captions[idx - 1].strip() if idx - 1 < len(captions) else ""
        if multi:
            label_text = f"【配图 {idx}（{cap}）】" if cap else f"【配图 {idx}】"
            user_parts.append({"type": "text", "text": label_text})
        elif cap:
            user_parts.append({"type": "text", "text": f"【配图 {idx}（{cap}）】"})

        data_url = _load_transposed_image_data_url(Path(assets_dir) / cas_img)
        user_parts.append({"type": "image_url", "image_url": {"url": data_url}})

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
) -> list[dict[str, Any]]:
    """构造定性/作图小问语义等价裁判消息（纯文本，字段白名单）。"""
    system_text = (
        "你是严谨的理科阅卷裁判。请判断两位解题者对同一小问的定性表述或作图操作方法是否在核心科学结论与方法上等价。\n"
        "请严格输出 JSON：{\"equivalent\": bool, \"confidence\": \"HIGH\"|\"MEDIUM\"|\"LOW\", \"reason\": \"...\"}。"
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
) -> list[dict[str, Any]]:
    """构造一题多解本质路线差异裁判消息（纯文本，字段白名单）。"""
    system_text = (
        "你是竞赛数学教研专家。请判断以下两份正确解答是否属于本质不同的解题路线（如几何割补构造 vs 代数方程/相似比例），"
        "而非仅仅是算式顺序或文字措辞的微小差异。\n"
        "请严格输出 JSON：{\"is_distinct_method\": bool, \"reason\": \"...\"}。"
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
) -> list[dict[str, Any]]:
    """构造复核页 [R] 辅助重算参考解答草稿消息（纯文本 text_llm，零视觉 Token，字段白名单）。"""
    system_text = (
        f"你是{grade_subject}学科解答重算助手。人工复核员已核定该题的题干与客观视觉事实（visual_facts），"
        "请严格依据提供的题干与 visual_facts 重新推导完整参考解答、各小问结构化答案（final_answers）与 SymPy 验算脚本（sympy_code）。\n"
        "小问标签仅允许 '主问' 或 '(1)', '(2)'...，禁止自造 '(1)-1' 子标签；sympy_code 必须定义 def verify(): 并通过真实运算得出结果。"
    )
    user_text = f"【学段学科】{grade_subject}\n\n【题干】\n{question_text}\n\n【已核定视觉事实 visual_facts】\n{visual_facts or '（无配图）'}"
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]
