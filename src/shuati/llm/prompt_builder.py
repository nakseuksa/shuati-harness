"""字段白名单 Prompt 构造器：严禁 uid / source_collection / chapter / 原始图片路径入 Prompt，内存 EXIF 矫正与多图显式编号。"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any, Literal

from PIL import Image, ImageOps

from shuati.models.schemas import Stage1Input

STAGE1_PROMPT_VERSION = "s1_v2"

_ANSWER_PART_SPEC_TEXT = """`final_answers` 为 `AnswerPart` 非空数组（**严禁改名为 `questions`、`answers` 或其他键**）。每个 `AnswerPart` 必须包含以下字段：
- `label`: 小问标签，仅允许 `"主问"` 或 `"(1)"`, `"(2)"`...；严禁自造 `"(1)-1"`、`"(1)-k"` 等子标签。
- `answer_type` 与 `verify_mode` 严格映射：
  - `NUMERIC` / `EXPRESSION` / `RATIO` / `SET_OR_INTERVAL` -> `verify_mode`: `"SYMBOLIC"`
  - `CHOICE` / `SYMBOL_FILL` -> `verify_mode`: `"EXACT"`
  - `TEXT` / `PROOF` -> `verify_mode`: `"QUALITATIVE"`
- `collection_mode`: `"SINGLE"` | `"ORDERED_TUPLE"` | `"UNORDERED_MULTISET"` | `"LABELED_MAP"`
- **关键约束（必读）**：
  1. 当 `collection_mode == "SINGLE"` 且 `verify_mode == "SYMBOLIC"` 时，`sympy_expr` **必须放在 `AnswerPart` 顶层**且为非空字符串，`items` 必须为 `null`——**严禁把 `sympy_expr` 放进 `items`**！
  2. 当 `collection_mode != "SINGLE"`（即 `ORDERED_TUPLE` / `UNORDERED_MULTISET` / `LABELED_MAP`）时，`items` **必须为非空数组**，`sympy_expr` 置 `null`。
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
        "\n7. `sympy_code` 规范（存在 `verify_mode == \"SYMBOLIC\"` 小问时必填非空字符串，否则置 `null`）："
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

    system_text = (
        f"你是一位严谨的{inp.grade_subject}学科解题专家。{role_desc}\n"
        "请严格输出一个合法的 JSON 对象（不要输出 Markdown 代码块包裹），并遵守以下硬性规则：\n"
        "1. JSON 字段顺序中 `visual_facts` 必须排在首位！若存在配图，`visual_facts` 须客观描述图中全部可见元素"
        "（顶点字母、线面位置关系、垂直/平行/等长/角度标注、阴影区域封闭边界与包含关系）；"
        "`visual_facts` 严禁写入原图未画出的辅助线、未标注的几何性质或任何推导结论。无图题 `visual_facts` 置空字符串 \"\"。\n"
        "2. 若图片模糊、标注缺失或阴影边界有歧义，必须将 `visual_ambiguity_flag` 设为 true 并在 `visual_ambiguity_note` 说明。\n"
        "3. 小问标签归一规范：题干有显式编号时统一使用 \"(1)\", \"(2)\"...；单问使用 \"主问\"；"
        "无编号“计算+说理”复合问拆为 \"(1)\" 与 \"(2)\"；严禁自造 \"(1)-1\"、\"(1)-k\" 等子小问标签！\n"
        "4. 单值小问使用 `collection_mode: \"SINGLE\"`（此时 `items` 为 `null`，且若为 `SYMBOLIC` 则 `sympy_expr` 必须放在 `AnswerPart` 顶层）；"
        "同一小问多空或多值使用 `ORDERED_TUPLE` / `UNORDERED_MULTISET` / `LABELED_MAP` 并装入非空 `items` 数组。\n"
        "5. `answer_type` 与 `verify_mode` 必须严格匹配："
        "NUMERIC/EXPRESSION/RATIO/SET_OR_INTERVAL -> SYMBOLIC；CHOICE/SYMBOL_FILL -> EXACT；TEXT/PROOF -> QUALITATIVE。\n"
        "6. 数值约定：题干未明确指定 π 近似值（如“π 取 3.14”）时，`sympy_expr` 必须保留精确符号 pi、sqrt(...) 或分数。"
        f"{sympy_rule}\n\n"
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
        "请严格输出一个合法的 JSON 对象（不要输出 Markdown 代码块包裹），遵守以下要求：\n"
        "1. 小问标签仅允许 \"主问\" 或 \"(1)\", \"(2)\"...，禁止自造 \"(1)-1\" 子标签。\n"
        "2. `sympy_code`（存在 `SYMBOLIC` 小问时必填，否则置 `null`）：仅允许导入 sympy / math / fractions，"
        "必须定义 def verify(): 返回 dict[str, str | list[str]]，并通过真实运算得出结果（禁止硬编码常量返回）。\n\n"
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


def build_taxonomy_propose_messages(
    *,
    grade_subject: str,
    clusters_summary: list[dict[str, Any]],
    top_pairs: list[dict[str, Any]],
    module_range: tuple[int, int] = (7, 10),
    l2_range: tuple[int, int] = (40, 70),
) -> list[dict[str, str]]:
    """Stage 3a：基于脱敏语义聚类摘要与高频词表归纳 L1/L2 标准目录树。"""
    system_text = (
        "你是理科课程体系与知识图谱专家。请根据给定学段学科的脱敏知识标签聚类摘要与高频原词榜，"
        f"自底向上归纳一套干净、互斥、层次清晰的二层分类词表（{module_range[0]}~{module_range[1]} 个一级大模块 L1，"
        f"合计 {l2_range[0]}~{l2_range[1]} 个二级知识实体 L2）。\n"
        "严格要求：\n"
        "1. 每个 L1 内部 L2 实体名称禁止重复或高度同义；所有别名收录在 `aliases`。\n"
        "2. 覆盖几何、数论、组合、应用题/行程、计算/代数等主要分支中实际出现的高频簇。\n"
        "3. 仅返回符合 TaxonomyProposeOutput 的合法 JSON 对象。"
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
) -> list[dict[str, str]]:
    """Stage 3c：脱敏逐题上下文批映射（禁止传入 question_uid / 来源 / 章节信息）。"""
    system_text = (
        "你是理科题库知识词表对齐专家。请结合题目抽象骨架与各步骤原始标签，"
        "将一批脱敏题目中的每一步映射至标准词表中的 `(mapped_l1, mapped_l2)`，并判定整题 `primary_l1_module` 与 `secondary_l1_modules`。\n"
        "严格要求：\n"
        "1. `primary_l1_module` 与每步 `mapped_l1` 必须严格取自给定标准词表的 L1。\n"
        "2. `mapped_l2` 优先取自对应 L1 下的标准 L2；仅当确实无可归入 L2 时才填写最接近或简明新 L2 名并设 `is_novel_l2=true`。\n"
        "3. 保留每题原始 `task_item_id` 与每步 `step_index`，返回符合 Stage3cBatchMappingOutput 的合法 JSON 对象。"
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


def build_merge_judge_messages(
    *,
    target_kind: str,
    scope_desc: str,
    candidate_pairs: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Stage 3d：L3 节点或全局概念候选对同义归并批裁判（零 question_uid）。"""
    system_text = (
        "你是理科知识点与解题概念去重裁判。给定同一严格作用域内的候选同义词对，"
        "请判断每一对是否本质上指代同一知识点/方法/母题。\n"
        "严格要求：\n"
        "1. 仅当两者数学含义等价、仅为表述/语序/简繁差异时设 `should_merge=true`，并给出最规范凝练的 `canonical_name`。\n"
        "2. 若存在关键条件、限定场景或适用对象差异（如“等高模型”vs“蝴蝶模型”），必须设 `should_merge=false`。\n"
        "3. 返回符合 MergeJudgeBatchOutput 的合法 JSON 对象。"
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
) -> list[dict[str, str]]:
    """Stage 3e：基于步序共现转移矩阵构建 L2 先修有向无环图（DAG）。"""
    system_text = (
        "你是理科课程先修依赖分析师。给定解题步骤中观察到的前置→后继 L2 转移频次与标准词表，"
        "请提炼真正具有教学先修关系（掌握 source 才能顺利学习/应用 target）的 L2 有向边列表。\n"
        "严格要求：\n"
        "1. `source_l1/source_l2` 与 `target_l1/target_l2` 必须严格取自标准词表。\n"
        "2. 严禁自环；严格保证边集构成有向无环图（DAG）。\n"
        "3. 返回符合 PrerequisiteDagOutput 的合法 JSON 对象。"
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
) -> list[dict[str, str]]:
    """Stage 4：针对 CONTRASTIVE 及边界带 ISOMORPHIC/SCAFFOLD 的脱敏精裁（零 question_uid）。"""
    system_text = (
        "你是理科题库教研拓扑质检员。请审核以下候选题目关系对（已脱敏，不含题号与来源）。\n"
        "判定标准：\n"
        "- CONTRASTIVE：表面情境/句式高度相似但核心解法或考点截然不同，适合做防定势辨析。\n"
        "- ISOMORPHIC：表皮不同或略有差异但核心数学结构与方法高度同构。\n"
        "- SCAFFOLD：B 为 A 的前置拆解或简化支撑（A 包含 B 的核心技能且难度显著更高）。\n"
        "请为每对给出 `is_valid`（true/false）及简明教学说明 `pedagogical_note`，返回 Stage4RefineBatchOutput JSON。"
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
