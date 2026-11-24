"""编译器提示词接缝、零 DB 校验工具、态势巡检与 MCP stdio CLI 命令组。"""

from __future__ import annotations

import json as json_lib
import sys
import time
from pathlib import Path
from typing import Any

import typer


def _read_json_payload(input_arg: str | None, use_stdin: bool) -> dict[str, Any]:
    from shuati.db.connection import PreconditionError

    if use_stdin and input_arg is not None:
        raise PreconditionError("--input 与 --stdin 不能同时传入")

    raw_text: str
    if use_stdin:
        raw_text = sys.stdin.read()
    elif input_arg is not None and input_arg.strip():
        s = input_arg.strip()
        if s.startswith("@"):
            fpath = Path(s[1:].strip())
            if not fpath.exists():
                raise PreconditionError(f"找不到 JSON 输入文件: {fpath}")
            raw_text = fpath.read_text(encoding="utf-8-sig")
        else:
            raw_text = s
    else:
        raise PreconditionError("必须提供 --input <JSON|@file> 或 --stdin")

    if not raw_text.strip():
        raise PreconditionError("JSON 输入载荷不能为空")
    try:
        parsed = json_lib.loads(raw_text)
    except ValueError as exc:
        raise PreconditionError(f"JSON 载荷解析失败: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PreconditionError("JSON 输入载荷顶层必须为对象 (dict)")
    return parsed


def _parse_optional_json_dict(raw: str | None) -> dict[str, Any] | None:
    from shuati.db.connection import PreconditionError

    if raw is None or not raw.strip():
        return None
    s = raw.strip()
    if s.startswith("@"):
        fpath = Path(s[1:].strip())
        if not fpath.exists():
            raise PreconditionError(f"找不到 JSON 参数文件: {fpath}")
        s = fpath.read_text(encoding="utf-8-sig")
    try:
        parsed = json_lib.loads(s)
    except ValueError as exc:
        raise PreconditionError(f"JSON 参数解析失败: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PreconditionError("JSON 参数顶层必须为对象 (dict)")
    return parsed


def register_compiler_commands(app: typer.Typer, run_cli_action: Any) -> None:
    """向主 Typer CLI 注册 prompt / tool / status / doctor / anchors / diagnose-preflight / mcp 命令。"""
    prompt_app = typer.Typer(name="prompt", help="三层提示词接缝治理（list / render / lint）")
    tool_app = typer.Typer(name="tool", help="零 DB 无状态自检核工具（verify-sympy / validate-draft）")
    anchors_app = typer.Typer(name="anchors", help="黄金难度锚点抽样与辅助工具")
    mcp_app = typer.Typer(name="mcp", help="角色物理隔离 stdio MCP 服务")

    app.add_typer(prompt_app)
    app.add_typer(tool_app)
    app.add_typer(anchors_app)
    app.add_typer(mcp_app)

    # 1. shuati prompt list / render / lint
    @prompt_app.command(name="list")
    def prompt_list_cmd(
        config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
        json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
    ) -> None:
        """列出 default / subjects / profiles / snippets 提示词接缝清单。"""
        from shuati.llm.seam_loader import list_prompt_seams

        run_cli_action("prompt.list", json, lambda: list_prompt_seams(config_dir=config_dir))

    @prompt_app.command(name="render")
    def prompt_render_cmd(
        stage: str = typer.Option(
            "unified",
            "--stage",
            help="目标阶段：unified / 1 (stage1/stage1_secondary) / stage1_judge / judge_multi / 2 (stage2) / 3a (stage3a) / 3c (stage3c) / 3d (stage3d) / merge_judge / 3e (stage3e) / 4 (stage4) / diagnose",
        ),
        grade_subject: str = typer.Option("小学奥数", "--grade-subject", help="学段学科"),
        prompt_profile: str | None = typer.Option(
            None, "--prompt-profile", help="可选 Layer 3 profile 名称（config/prompts/profiles/<name>.yaml）"
        ),
        sample: str | None = typer.Option(
            None, "--sample", help="可选样例载荷 JSON 或 @文件路径"
        ),
        config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
        json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
    ) -> None:
        """渲染合并后的完整 Prompt 预览、摘要与扁平化 Schema。"""
        from shuati.llm.prompt_builder import render_prompt_preview

        def _work() -> dict[str, Any]:
            sample_obj = _parse_optional_json_dict(sample)
            return render_prompt_preview(
                stage=stage,
                grade_subject=grade_subject,
                config_dir=config_dir,
                prompt_profile=prompt_profile,
                sample_payload=sample_obj,
            )

        run_cli_action("prompt.render", json, _work)

    @prompt_app.command(name="lint")
    def prompt_lint_cmd(
        config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
        json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
    ) -> None:
        """静态校验提示词接缝必填槽位、继承可合并性与外链路径安全性。"""
        from shuati.llm.seam_loader import lint_prompt_seams

        run_cli_action("prompt.lint", json, lambda: lint_prompt_seams(config_dir=config_dir))

    # 2. shuati tool verify-sympy & validate-draft
    @tool_app.command(name="verify-sympy")
    def tool_verify_sympy_cmd(
        input_arg: str | None = typer.Option(
            None, "--input", help="JSON 载荷或 @文件路径（避免 PowerShell 引号转义）"
        ),
        stdin: bool = typer.Option(False, "--stdin", help="从标准输入读取 JSON 载荷"),
        grade_subject: str | None = typer.Option(
            None, "--grade-subject", help="覆盖学段学科（默认读取载荷或 小学奥数）"
        ),
        config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
        json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
    ) -> None:
        """零 DB 无状态执行 AnswerPart 归一与 SymPy 沙盒验算。"""
        from shuati.db.connection import PreconditionError
        from shuati.verify.draft_validator import verify_sympy_payload

        def _work() -> dict[str, Any]:
            payload = _read_json_payload(input_arg, stdin)
            final_answers = payload.get("final_answers")
            if not isinstance(final_answers, list):
                raise PreconditionError("载荷必须包含数组字段 final_answers")
            options = payload.get("options")
            if options is not None and not isinstance(options, dict):
                raise PreconditionError("载荷字段 options 必须为对象或 null")
            gs = grade_subject or str(payload.get("grade_subject") or "小学奥数")
            return verify_sympy_payload(
                sympy_code=payload.get("sympy_code"),
                final_answers=final_answers,
                grade_subject=gs,
                options={str(k): str(v) for k, v in options.items()} if options else None,
                config_dir=config_dir,
            )

        run_cli_action("tool.verify-sympy", json, _work)

    @tool_app.command(name="validate-draft")
    def tool_validate_draft_cmd(
        input_arg: str | None = typer.Option(
            None, "--input", help="JSON 载荷或 @文件路径（避免 PowerShell 引号转义）"
        ),
        stdin: bool = typer.Option(False, "--stdin", help="从标准输入读取 JSON 载荷"),
        mode: str | None = typer.Option(
            None, "--mode", help="校验模式：UNIFIED_S1_S2 / STAGE2_ONLY / DIAGNOSE"
        ),
        grade_subject: str | None = typer.Option(
            None, "--grade-subject", help="覆盖学段学科（默认读取载荷或 小学奥数）"
        ),
        run_sympy: bool = typer.Option(
            True, "--run-sympy/--no-run-sympy", help="是否同时执行 SymPy 沙盒验算"
        ),
        config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
        json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
    ) -> None:
        """零 DB 无状态执行草稿全量自检核（Schema + 规则 + 可选 SymPy）。"""
        from shuati.db.connection import PreconditionError
        from shuati.verify.draft_validator import validate_draft_payload

        def _work() -> dict[str, Any]:
            payload = _read_json_payload(input_arg, stdin)
            mode_val = (mode or str(payload.get("mode") or "UNIFIED_S1_S2")).strip().upper()
            if mode_val not in ("UNIFIED_S1_S2", "STAGE2_ONLY", "DIAGNOSE"):
                raise PreconditionError(f"不支持的校验模式: {mode_val!r}")
            raw_cand = payload.get("candidate")
            candidate: dict[str, Any] = raw_cand if isinstance(raw_cand, dict) else payload
            question_text = str(payload.get("question_text") or "")
            gs = grade_subject or str(payload.get("grade_subject") or "小学奥数")
            options = payload.get("options")
            if options is not None and not isinstance(options, dict):
                raise PreconditionError("载荷字段 options 必须为对象或 null")
            verified_answers = payload.get("verified_final_answers")
            if verified_answers is not None and not isinstance(verified_answers, list):
                raise PreconditionError("载荷字段 verified_final_answers 必须为数组或 null")
            return validate_draft_payload(
                mode=mode_val,  # type: ignore[arg-type]
                candidate=candidate,
                question_text=question_text,
                grade_subject=gs,
                options={str(k): str(v) for k, v in options.items()} if options else None,
                verified_final_answers=verified_answers,
                run_sympy=run_sympy,
                config_dir=config_dir,
            )

        run_cli_action("tool.validate-draft", json, _work)

    # 3. shuati status
    @app.command(name="status")
    def status_cmd(
        grade_subject: str | None = typer.Option(None, "--grade-subject", help="限定学段学科"),
        db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
        config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
        vectors_dir: Path = typer.Option(Path("data/vectors"), "--vectors-dir", help="向量仓目录"),
        unified_dir: Path = typer.Option(Path("data/unified"), "--unified-dir", help="批任务目录"),
        json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
    ) -> None:
        """只读巡检流水线全景态势（Stage 0~4 矩阵、挂起批、向量漂移与下一步建议）。"""
        from shuati.services.compiler_tools import get_pipeline_status

        run_cli_action(
            "status",
            json,
            lambda: get_pipeline_status(
                db_path=db,
                config_dir=config_dir,
                vectors_dir=vectors_dir,
                unified_dir=unified_dir,
                grade_subject=grade_subject,
            ),
        )

    # 4. shuati diagnose-preflight
    @app.command(name="diagnose-preflight")
    def diagnose_preflight_cmd(
        file: Path = typer.Option(..., "--file", help="待预检的 Markdown 题目列表文件（只读）"),
        out_normalized: Path | None = typer.Option(
            None, "--out-normalized", help="规范化副本输出路径（默认 data/drafts/preflight/）"
        ),
        drafts_dir: Path = typer.Option(Path("data/drafts"), "--drafts-dir", help="草稿目录"),
        json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
    ) -> None:
        """只读预检并生成规范化题单副本（补空格、图行归位、同行正文拆分与绝对路径解析）。"""
        from shuati.services.diagnose_batch import preflight_question_list_markdown

        run_cli_action(
            "diagnose-preflight",
            json,
            lambda: preflight_question_list_markdown(
                file,
                out_normalized=out_normalized,
                drafts_dir=drafts_dir,
            ),
        )

    # 5. shuati mcp serve
    @mcp_app.command(name="serve")
    def mcp_serve_cmd(
        role: str = typer.Option(
            "worker",
            "--role",
            help="角色物理隔离档位：worker / compiler / practice / all",
        ),
        db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
        config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
        vectors_dir: Path = typer.Option(Path("data/vectors"), "--vectors-dir", help="向量仓目录"),
        assets_dir: Path = typer.Option(Path("data/assets/images"), "--assets-dir", help="CAS 图片目录"),
        unified_dir: Path = typer.Option(Path("data/unified"), "--unified-dir", help="批任务目录"),
        human_dir: Path = typer.Option(Path("human"), "--human-dir", help="人工镜像目录"),
        drafts_dir: Path = typer.Option(Path("data/drafts"), "--drafts-dir", help="草稿目录"),
    ) -> None:
        """启动换行分隔的 UTF-8 JSON-RPC 2.0 stdio MCP 服务。"""
        from shuati.mcp.server import serve_stdio
        from shuati.models.domain import EXIT_PRECONDITION

        norm_role = role.strip().lower()
        if norm_role not in ("worker", "compiler", "practice", "all"):
            print(f"[PRECONDITION_FAILED] 无效 --role: {role!r}", file=sys.stderr)
            raise typer.Exit(code=EXIT_PRECONDITION)
        code = serve_stdio(
            role=norm_role,  # type: ignore[arg-type]
            db_path=db,
            config_dir=config_dir,
            vectors_dir=vectors_dir,
            assets_dir=assets_dir,
            unified_dir=unified_dir,
            human_dir=human_dir,
            drafts_dir=drafts_dir,
        )
        if code != 0:
            raise typer.Exit(code=code)

    # 6. shuati doctor
    @app.command(name="doctor")
    def doctor_cmd(
        check_llm_api: bool = typer.Option(
            False, "--check-llm-api", help="是否探测 LLM API 槽位连通性（Swarm 免 Key 模式可跳过）"
        ),
        mock: bool | None = typer.Option(None, "--mock/--no-mock", help="使用离线 Mock Embedding / LLM 探针"),
        db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
        config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
        json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
    ) -> None:
        """冷启动与环境健康自检（无需已存在数据库；实测 SymPy 沙盒与真实 Embedding 维度）。"""
        from shuati.cli.main import output_envelope
        from shuati.models.domain import EXIT_INTERNAL, EXIT_OK, EXIT_PRECONDITION, ErrorDetail
        from shuati.services.compiler_tools import run_doctor

        t0 = time.time()
        try:
            res = run_doctor(
                db_path=db,
                config_dir=config_dir,
                check_llm_api=check_llm_api,
                use_mock=mock,
            )
            duration_ms = int((time.time() - t0) * 1000)
            warnings = [str(w) for w in res.get("warnings", [])]
            if res.get("ok"):
                output_envelope(
                    "doctor",
                    data=res,
                    warnings=warnings,
                    meta={"duration_ms": duration_ms},
                    as_json=json,
                    exit_code=EXIT_OK,
                )
            else:
                err_msg = "; ".join(str(e) for e in res.get("errors", [])) or "环境健康自检未通过"
                output_envelope(
                    "doctor",
                    data=res,
                    warnings=warnings,
                    error=ErrorDetail(code="PRECONDITION_FAILED", message=err_msg),
                    meta={"duration_ms": duration_ms},
                    as_json=json,
                    exit_code=EXIT_PRECONDITION,
                )
        except typer.Exit:
            raise
        except Exception as exc:
            duration_ms = int((time.time() - t0) * 1000)
            output_envelope(
                "doctor",
                error=ErrorDetail(code="INTERNAL_ERROR", message=str(exc)),
                meta={"duration_ms": duration_ms},
                as_json=json,
                exit_code=EXIT_INTERNAL,
            )

    # 7. shuati anchors sample
    @anchors_app.command(name="sample")
    def anchors_sample_cmd(
        grade_subject: str = typer.Option(..., "--grade-subject", help="目标学段学科"),
        count: int = typer.Option(6, "--count", min=1, help="分层抽样候选锚点题数"),
        db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
        json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
    ) -> None:
        """只读分层抽样候选黄金难度锚点题目（跨长度分位与有图/无图，严禁泄露章节）。"""
        from shuati.services.compiler_tools import sample_anchor_candidates

        run_cli_action(
            "anchors.sample",
            json,
            lambda: sample_anchor_candidates(
                db_path=db,
                grade_subject=grade_subject,
                count=count,
            ),
        )
