"""M3 交互与 pi 一等公民 CLI 命令集（DESIGN.md §5.4-4A）。"""

from __future__ import annotations

import asyncio
import json as json_lib
import os
import shutil
from pathlib import Path
from typing import Any

import typer

from shuati.config import load_settings
from shuati.core import effective
from shuati.db.connection import PreconditionError, get_connection
from shuati.db.schema import init_db
from shuati.services import facade
from shuati.services.reveal import RevealMode, apply_reveal


def _parse_json_arg(raw: str | None) -> Any:
    if raw is None or not raw.strip():
        return None
    s = raw.strip()
    if s.startswith("@"):
        fpath = Path(s[1:].strip())
        if not fpath.exists():
            raise PreconditionError(f"找不到 JSON 参数文件: {fpath}")
        s = fpath.read_text(encoding="utf-8")
    try:
        return json_lib.loads(s)
    except ValueError as exc:
        raise PreconditionError(f"JSON 参数解析失败: {exc}") from exc


def _validate_reveal(reveal: str) -> RevealMode:
    val = reveal.strip().lower()
    if val not in ("quiz", "hint", "full"):
        raise PreconditionError(f"--reveal 仅支持 quiz / hint / full，收到 {reveal!r}")
    return val  # type: ignore[return-value]


def register_m3_commands(
    app: typer.Typer,
    taxonomy_app: typer.Typer,
    run_cli_action: Any,
) -> None:
    """向主 Typer CLI 注册 M3 全量交互命令。"""
    learner_app = typer.Typer(name="learner", help="学习者角色管理")
    skill_app = typer.Typer(name="skill", help="pi Skill 安装与同步")
    app.add_typer(learner_app)
    app.add_typer(skill_app)

    # 1. shuati taxonomy（无子命令时浏览知识树 + 先修 DAG + 热力）
    @taxonomy_app.callback(invoke_without_command=True)
    def taxonomy_browse_callback(
        ctx: typer.Context,
        grade_subject: str = typer.Option("小学奥数", "--grade-subject", help="目标学段学科"),
        learner: str | None = typer.Option(None, "--learner", help="学习者 ID（叠加掌握热力）"),
        l1: str | None = typer.Option(None, "--l1", help="限定一级大模块 L1"),
        db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
        config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
        json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
    ) -> None:
        if ctx.invoked_subcommand is not None:
            return

        def _work() -> dict[str, Any]:
            settings = load_settings(config_dir / "settings.yaml")
            conn = get_connection(db, readonly=True)
            try:
                return facade.browse_taxonomy(
                    conn,
                    grade_subject=grade_subject,
                    learner_id=learner,
                    l1=l1,
                    settings=settings,
                )
            finally:
                conn.close()

        run_cli_action("taxonomy", json, _work)

    # 2. shuati search
    @app.command(name="search")
    def search_cmd(
        grade_subject: str | None = typer.Option(None, "--grade-subject", help="限定学段学科"),
        l1: str | None = typer.Option(None, "--l1", help="按 L1 模块过滤"),
        l2: str | None = typer.Option(None, "--l2", help="按 L2 实体过滤"),
        l3: str | None = typer.Option(None, "--l3", help="按 L3 节点/算子过滤"),
        motif: str | None = typer.Option(None, "--motif", help="按经典母题过滤"),
        method: str | None = typer.Option(None, "--method", help="按思维方法过滤"),
        trap: str | None = typer.Option(None, "--trap", help="按易错陷阱过滤"),
        tier: str | None = typer.Option(None, "--tier", help="按 8 档等级过滤"),
        elo_min: int | None = typer.Option(None, "--elo-min", help="难度下界"),
        elo_max: int | None = typer.Option(None, "--elo-max", help="难度上界"),
        query: str | None = typer.Option(None, "--query", help="FTS5 关键词检索"),
        semantic: str | None = typer.Option(None, "--semantic", help="按解题骨架语义检索"),
        exclude_solved: bool = typer.Option(False, "--exclude-solved", help="排除已独立做对题目"),
        learner: str | None = typer.Option(None, "--learner", help="指定学习者 ID"),
        reveal: str = typer.Option("quiz", "--reveal", help="防剧透档位：quiz / hint / full"),
        hint_level: int = typer.Option(1, "--hint-level", help="hint 档展示步数"),
        include_visual_facts: bool = typer.Option(False, "--include-visual-facts", help="quiz 档显式附带 visual_facts"),
        limit: int = typer.Option(20, "--limit", help="返回上限"),
        mock: bool | None = typer.Option(None, "--mock/--no-mock", help="使用离线 Mock Embedding"),
        db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
        config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
        vectors_dir: Path = typer.Option(Path("data/vectors"), "--vectors-dir", help="向量仓目录"),
        assets_dir: Path = typer.Option(Path("data/assets/images"), "--assets-dir", help="CAS 图片目录"),
        json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
    ) -> None:
        """多维条件 + FTS5 + 语义检索题目（默认防剧透 reveal=quiz）。"""

        def _work() -> dict[str, Any]:
            rev_mode = _validate_reveal(reveal)
            settings = load_settings(config_dir / "settings.yaml")
            conn = get_connection(db, readonly=True)
            try:
                return facade.search_questions(
                    conn,
                    settings,
                    grade_subject=grade_subject,
                    l1=l1,
                    l2=l2,
                    l3=l3,
                    motif=motif,
                    method=method,
                    trap=trap,
                    tier=tier,
                    elo_min=elo_min,
                    elo_max=elo_max,
                    query=query,
                    semantic=semantic,
                    exclude_solved=exclude_solved,
                    learner_id=learner,
                    reveal=rev_mode,
                    hint_level=hint_level,
                    include_visual_facts=include_visual_facts,
                    limit=limit,
                    assets_dir=assets_dir,
                    vectors_dir=vectors_dir,
                    use_mock=mock,
                )
            finally:
                conn.close()

        run_cli_action("search", json, _work)

    # 3. shuati related <uid>
    @app.command(name="related")
    def related_cmd(
        uid: str = typer.Argument(..., help="题目 question_uid 或 source_id 简写"),
        types: str | None = typer.Option(None, "--types", help="逗号分隔边类型，如 SCAFFOLD,ISOMORPHIC"),
        direction: str = typer.Option("both", "--direction", help="SCAFFOLD 方向：pre(子题) / post(母题) / both"),
        exclude_solved: bool = typer.Option(False, "--exclude-solved", help="排除已独立做对题目"),
        learner: str | None = typer.Option(None, "--learner", help="指定学习者 ID"),
        reveal: str = typer.Option("quiz", "--reveal", help="防剧透档位：quiz / hint / full"),
        hint_level: int = typer.Option(1, "--hint-level", help="hint 档展示步数"),
        include_visual_facts: bool = typer.Option(False, "--include-visual-facts", help="quiz 档附带 visual_facts"),
        limit: int = typer.Option(20, "--limit", help="返回上限"),
        db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
        assets_dir: Path = typer.Option(Path("data/assets/images"), "--assets-dir", help="CAS 图片目录"),
        json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
    ) -> None:
        """沿五边拓扑查询前置铺垫、升维母题、同构变式、形似神异与同坑排雷题。"""

        def _work() -> dict[str, Any]:
            rev_mode = _validate_reveal(reveal)
            type_list = [t.strip() for t in types.split(",") if t.strip()] if types else None
            conn = get_connection(db, readonly=True)
            try:
                return facade.get_related_questions(
                    conn,
                    uid,
                    types=type_list,
                    direction=direction,
                    exclude_solved=exclude_solved,
                    learner_id=learner,
                    reveal=rev_mode,
                    hint_level=hint_level,
                    include_visual_facts=include_visual_facts,
                    limit=limit,
                    assets_dir=assets_dir,
                )
            finally:
                conn.close()

        run_cli_action("related", json, _work)

    # 4. shuati compose-quiz
    @app.command(name="compose-quiz")
    def compose_quiz_cmd(
        grade_subject: str = typer.Option("小学奥数", "--grade-subject", help="目标学段学科"),
        learner: str | None = typer.Option(None, "--learner", help="学习者 ID（默认当前角色）"),
        l1: str | None = typer.Option(None, "--l1", help="按 L1 模块过滤"),
        l2: str | None = typer.Option(None, "--l2", help="按 L2 实体过滤"),
        l3: str | None = typer.Option(None, "--l3", help="按 L3 节点/算子过滤"),
        motif: str | None = typer.Option(None, "--motif", help="按经典母题过滤"),
        method: str | None = typer.Option(None, "--method", help="按思维方法过滤"),
        trap: str | None = typer.Option(None, "--trap", help="按易错陷阱过滤"),
        count: int = typer.Option(5, "--count", help="组卷总题数"),
        mix: str = typer.Option("2:2:1", "--mix", help="稳固:拔高:排雷 比例"),
        exclude_solved_days: int | None = typer.Option(None, "--exclude-solved-days", help="已做对冷却天数"),
        exclude_clone_days: int | None = typer.Option(None, "--exclude-clone-days", help="CLONE 邻居冷却天数"),
        exclude_recent_quiz_batches: int | None = typer.Option(None, "--exclude-recent-quiz-batches", help="排除最近 N 份未作答卷"),
        target_elo: int | None = typer.Option(None, "--target-elo", help="手动覆盖基准水位"),
        reveal: str = typer.Option("quiz", "--reveal", help="返回题面防剧透档位：quiz / hint / full"),
        include_visual_facts: bool = typer.Option(False, "--include-visual-facts", help="quiz 档附带 visual_facts"),
        db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
        config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
        assets_dir: Path = typer.Option(Path("data/assets/images"), "--assets-dir", help="CAS 图片目录"),
        json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
    ) -> None:
        """按角色分模块水位一键梯度组专题小卷，并持久化 quiz_batches。"""

        def _work() -> dict[str, Any]:
            rev_mode = _validate_reveal(reveal)
            settings = load_settings(config_dir / "settings.yaml")
            conn = get_connection(db)
            try:
                init_db(conn, config_dir=config_dir)
                lid = facade.resolve_learner_id(conn, learner)
                res = facade.compose_quiz(
                    conn,
                    learner_id=lid,
                    grade_subject=grade_subject,
                    elo_cfg=settings.learner_elo,
                    composer_cfg=settings.composer,
                    l1=l1,
                    l2=l2,
                    l3=l3,
                    motif=motif,
                    method=method,
                    trap=trap,
                    count=count,
                    mix=mix,
                    exclude_solved_days=exclude_solved_days,
                    exclude_clone_days=exclude_clone_days,
                    exclude_recent_quiz_batches=exclude_recent_quiz_batches,
                    target_elo=target_elo,
                )
                hydrated_items: list[dict[str, Any]] = []
                for it in res["items"]:
                    raw_d = facade.hydrate_question_dict(
                        conn,
                        it["question_uid"],
                        assets_dir=assets_dir,
                        reason_public=it["reason_public"],
                        reason_full=it["reason_full"],
                    )
                    trimmed = apply_reveal(
                        raw_d,
                        reveal=rev_mode,
                        include_visual_facts=include_visual_facts,
                    )
                    trimmed["slot"] = it["slot"]
                    hydrated_items.append(trimmed)
                res["items"] = hydrated_items
                return res
            finally:
                conn.close()

        run_cli_action("compose-quiz", json, _work)

    # 5. shuati detail <uid>
    @app.command(name="detail")
    def detail_cmd(
        uid: str = typer.Argument(..., help="题目 question_uid 或 source_id 简写"),
        reveal: str = typer.Option("quiz", "--reveal", help="防剧透档位：quiz / hint / full"),
        hint_level: int = typer.Option(1, "--hint-level", help="hint 档展示前 k 步提示"),
        include_visual_facts: bool = typer.Option(False, "--include-visual-facts", help="quiz 档显式附带 visual_facts"),
        db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
        assets_dir: Path = typer.Option(Path("data/assets/images"), "--assets-dir", help="CAS 图片目录"),
        json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
    ) -> None:
        """按防剧透档位查看单题信息。"""

        def _work() -> dict[str, Any]:
            rev_mode = _validate_reveal(reveal)
            conn = get_connection(db, readonly=True)
            try:
                return facade.get_question_detail(
                    conn,
                    uid,
                    assets_dir=assets_dir,
                    reveal=rev_mode,
                    hint_level=hint_level,
                    include_visual_facts=include_visual_facts,
                )
            finally:
                conn.close()

        run_cli_action("detail", json, _work)

    # 6. shuati check <uid>
    @app.command(name="check")
    def check_cmd(
        uid: str = typer.Argument(..., help="题目 question_uid 或 source_id 简写"),
        answer: str = typer.Option(..., "--answer", help="学习者提交答案（支持 '(1)9:4;(2)156' 或单值）"),
        learner: str | None = typer.Option(None, "--learner", help="指定学习者 ID"),
        hint_level: int = typer.Option(0, "--hint-level", help="本次做题用过的最高提示层级"),
        log: bool = typer.Option(False, "--log", help="判定后同步登记 practice_log"),
        request_id: str | None = typer.Option(None, "--request-id", help="幂等写入请求 ID"),
        quiz_batch_id: str | None = typer.Option(None, "--quiz-batch-id", help="关联组卷批次 ID"),
        judge_verdict: str | None = typer.Option(None, "--judge-verdict", help="定性小问裁判 JSON 或 @文件逃生通道"),
        mock: bool | None = typer.Option(None, "--mock/--no-mock", help="使用离线 Mock 裁判"),
        db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
        config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
        human_dir: Path = typer.Option(Path("human"), "--human-dir", help="人工镜像目录"),
        json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
    ) -> None:
        """不泄露标准答案的逐小问判题（仅输出 verdict 与通过率统计）。"""

        def _work() -> dict[str, Any]:
            settings = load_settings(config_dir / "settings.yaml")
            j_obj = _parse_json_arg(judge_verdict)
            conn = get_connection(db, readonly=not log)
            try:
                if log:
                    init_db(conn, config_dir=config_dir)
                return asyncio.run(
                    facade.check_answer_async(
                        conn,
                        settings,
                        uid,
                        answer_input=answer,
                        learner_id=learner,
                        hint_level=hint_level,
                        log=log,
                        request_id=request_id,
                        quiz_batch_id=quiz_batch_id,
                        judge_override_json=j_obj,
                        human_dir=human_dir,
                        use_mock=mock,
                    )
                )
            finally:
                conn.close()

        run_cli_action("check", json, _work)

    # 7. shuati diagnose
    @app.command(name="diagnose")
    def diagnose_cmd(
        text: str | None = typer.Option(None, "--text", help="外部新错题题干文本（与 --file 二选一互斥）"),
        file: Path | None = typer.Option(None, "--file", help="批量诊断题目列表文件（Markdown / 纯文本，与 --text 互斥）"),
        image: list[Path] = typer.Option([], "--image", help="外部错题本地图片路径（可多次传入，仅单题 --text 模式）"),
        grade_subject: str = typer.Option("小学奥数", "--grade-subject", help="学段学科"),
        answer: str | None = typer.Option(None, "--answer", help="可选已知标准答案用于一致性比对（仅单题模式）"),
        from_json: str | None = typer.Option(None, "--from-json", help="结构化诊断 JSON 或 @文件逃生通道（仅单题模式）"),
        concurrency: int = typer.Option(1, "--concurrency", min=1, help="批量诊断并发数（默认 1 逐题执行）"),
        force: bool = typer.Option(False, "--force", help="批量诊断时忽略已有草稿缓存，强制重跑 VLM"),
        mock: bool | None = typer.Option(None, "--mock/--no-mock", help="使用离线 Mock VLM/Embedding"),
        db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
        config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
        vectors_dir: Path = typer.Option(Path("data/vectors"), "--vectors-dir", help="向量仓目录"),
        drafts_dir: Path = typer.Option(Path("data/drafts"), "--drafts-dir", help="临时草稿目录"),
        json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
    ) -> None:
        """只读实时诊断外部新错题（支持 --text 单题或 --file 批量 Markdown 列表），输出推荐并缓存 draft_id。"""

        def _work() -> dict[str, Any]:
            if (text is None and file is None) or (text is not None and file is not None):
                raise PreconditionError("必须且仅能提供 --text 或 --file 之一")
            settings = load_settings(config_dir / "settings.yaml")
            conn = get_connection(db, readonly=True)
            try:
                if file is not None:
                    if image or answer is not None or from_json is not None:
                        raise PreconditionError("--file 批量模式不支持同时传入 --image / --answer / --from-json")
                    return asyncio.run(
                        facade.diagnose_external_file_async(
                            conn,
                            settings,
                            file_path=file,
                            grade_subject=grade_subject,
                            concurrency=concurrency,
                            force=force,
                            config_dir=config_dir,
                            vectors_dir=vectors_dir,
                            drafts_dir=drafts_dir,
                            use_mock=mock,
                        )
                    )

                assert text is not None
                diag_obj = _parse_json_arg(from_json)
                return asyncio.run(
                    facade.diagnose_external_mistake_async(
                        conn,
                        settings,
                        question_text=text,
                        grade_subject=grade_subject,
                        image_paths=image,
                        known_answer=answer,
                        diagnosis_json=diag_obj,
                        config_dir=config_dir,
                        vectors_dir=vectors_dir,
                        drafts_dir=drafts_dir,
                        use_mock=mock,
                    )
                )
            finally:
                conn.close()

        run_cli_action("diagnose", json, _work)

    # 8. shuati ingest-mistake
    @app.command(name="ingest-mistake")
    def ingest_mistake_cmd(
        draft_id: str | None = typer.Option(None, "--draft-id", help="复用 diagnose 产出的草稿 ID"),
        text: str | None = typer.Option(None, "--text", help="外部新错题题干（无 draft-id 时必填）"),
        image: list[Path] = typer.Option([], "--image", help="外部错题图片路径"),
        grade_subject: str | None = typer.Option(None, "--grade-subject", help="学段学科"),
        answer: str | None = typer.Option(None, "--answer", help="已知标准答案（作为 KNOWN_ANSWER 核验）"),
        from_json: str | None = typer.Option(None, "--from-json", help="主路结构化诊断 JSON 或 @文件"),
        secondary_json: str | None = typer.Option(None, "--secondary-json", help="辅路双盲 JSON 或 @文件逃生通道"),
        log_wrong: bool = typer.Option(False, "--log-wrong", help="入库后同步写入一条 WRONG 做题日志"),
        learner: str = typer.Option("default", "--learner", help="关联学习者 ID"),
        note: str = typer.Option("", "--note", help="错题笔记"),
        dry_run: bool = typer.Option(False, "--dry-run", help="仅预览变更 diff，不写库"),
        mock: bool | None = typer.Option(None, "--mock/--no-mock", help="使用离线 Mock 推理与向量"),
        db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
        config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
        assets_dir: Path = typer.Option(Path("data/assets/images"), "--assets-dir", help="CAS 图片目录"),
        vectors_dir: Path = typer.Option(Path("data/vectors"), "--vectors-dir", help="向量仓目录"),
        drafts_dir: Path = typer.Option(Path("data/drafts"), "--drafts-dir", help="临时草稿目录"),
        human_dir: Path = typer.Option(Path("human"), "--human-dir", help="人工镜像目录"),
        json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
    ) -> None:
        """将外部新错题一键写入题库、补齐双盲验真/Stage2/S3/五边并快照至 human/external_questions.jsonl。"""

        def _work() -> dict[str, Any]:
            settings = load_settings(config_dir / "settings.yaml")
            diag_obj = _parse_json_arg(from_json)
            sec_obj = _parse_json_arg(secondary_json)
            conn = get_connection(db)
            try:
                init_db(conn, config_dir=config_dir)
                effective.install_eff_write_guard(conn)
                return asyncio.run(
                    facade.ingest_external_mistake_async(
                        conn,
                        settings,
                        draft_id=draft_id,
                        question_text=text,
                        grade_subject=grade_subject,
                        image_paths=image,
                        known_answer=answer,
                        diagnosis_json=diag_obj,
                        secondary_json=sec_obj,
                        log_wrong=log_wrong,
                        learner_id=learner,
                        user_note=note,
                        dry_run=dry_run,
                        assets_dir=assets_dir,
                        vectors_dir=vectors_dir,
                        drafts_dir=drafts_dir,
                        human_dir=human_dir,
                        lock_path=db.parent / ".compile.lock",
                        use_mock=mock,
                    )
                )
            finally:
                conn.close()

        run_cli_action("ingest-mistake", json, _work)

    # 9. shuati log / log-batch / log-void
    @app.command(name="log")
    def log_cmd(
        uid: str = typer.Argument(..., help="题目 question_uid 或 source_id"),
        outcome: str = typer.Option(..., "--outcome", help="SOLVED_SOLO / SOLVED_WITH_HINT / WRONG / SKIPPED"),
        learner: str | None = typer.Option(None, "--learner", help="学习者 ID"),
        score_ratio: float | None = typer.Option(None, "--score-ratio", help="部分得分比例 [0.0, 1.0]"),
        stuck_step: int | None = typer.Option(None, "--stuck-step", help="卡住的步骤序号（自动快照节点）"),
        hint_level: int | None = typer.Option(None, "--hint-level", help="使用提示等级"),
        note: str = typer.Option("", "--note", help="备注"),
        answer: str | None = typer.Option(None, "--answer", help="学习者作答原文"),
        quiz_batch_id: str | None = typer.Option(None, "--quiz-batch-id", help="关联组卷批次"),
        request_id: str | None = typer.Option(None, "--request-id", help="幂等写入请求 ID"),
        practiced_at: str | None = typer.Option(None, "--practiced-at", help="作答时间（UTC ISO 字符串）"),
        db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
        config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
        human_dir: Path = typer.Option(Path("human"), "--human-dir", help="人工镜像目录"),
        json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
    ) -> None:
        """单题做题足迹登记（幂等 + 卡点快照 + 镜像同步）。"""

        def _work() -> dict[str, Any]:
            settings = load_settings(config_dir / "settings.yaml")
            conn = get_connection(db)
            try:
                init_db(conn, config_dir=config_dir)
                return facade.log_practice(
                    conn,
                    uid_or_id=uid,
                    outcome=outcome,
                    learner_id=learner,
                    score_ratio=score_ratio,
                    stuck_step_index=stuck_step,
                    hint_level_used=hint_level,
                    user_note=note,
                    submitted_answer=answer,
                    quiz_batch_id=quiz_batch_id,
                    request_id=request_id,
                    practiced_at=practiced_at,
                    human_dir=human_dir,
                    include_practice_log=settings.human_mirror.include_practice_log,
                )
            finally:
                conn.close()

        run_cli_action("log", json, _work)

    @app.command(name="log-batch")
    def log_batch_cmd(
        quiz_batch_id: str = typer.Option(..., "--quiz-batch-id", help="组卷批次 ID"),
        entries: str = typer.Option(..., "--entries", help="JSON 数组或 @文件路径，含 [{uid, outcome, ...}]"),
        learner: str | None = typer.Option(None, "--learner", help="覆盖学习者 ID"),
        request_id: str | None = typer.Option(None, "--request-id", help="批量请求 ID 前缀"),
        db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
        config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
        human_dir: Path = typer.Option(Path("human"), "--human-dir", help="人工镜像目录"),
        json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
    ) -> None:
        """按 quiz_batch_id 整卷批量回填做题记录。"""

        def _work() -> dict[str, Any]:
            settings = load_settings(config_dir / "settings.yaml")
            parsed_entries = _parse_json_arg(entries)
            if not isinstance(parsed_entries, list):
                raise PreconditionError("--entries 必须为 JSON 数组")
            conn = get_connection(db)
            try:
                init_db(conn, config_dir=config_dir)
                return facade.log_batch_practice(
                    conn,
                    quiz_batch_id=quiz_batch_id,
                    entries=parsed_entries,
                    learner_id=learner,
                    request_id_prefix=request_id,
                    human_dir=human_dir,
                    include_practice_log=settings.human_mirror.include_practice_log,
                )
            finally:
                conn.close()

        run_cli_action("log-batch", json, _work)

    @app.command(name="log-void")
    def log_void_cmd(
        log_id: int = typer.Argument(..., help="需要冲销的 practice_log.log_id"),
        reason: str = typer.Option("", "--reason", help="冲销原因说明"),
        db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
        config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
        human_dir: Path = typer.Option(Path("human"), "--human-dir", help="人工镜像目录"),
        json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
    ) -> None:
        """只增不删地冲销误记的做题记录。"""

        def _work() -> dict[str, Any]:
            settings = load_settings(config_dir / "settings.yaml")
            conn = get_connection(db)
            try:
                init_db(conn, config_dir=config_dir)
                return facade.void_practice_log(
                    conn,
                    log_id,
                    reason=reason,
                    human_dir=human_dir,
                    include_practice_log=settings.human_mirror.include_practice_log,
                )
            finally:
                conn.close()

        run_cli_action("log-void", json, _work)

    # 10. shuati profile & shuati learner
    @app.command(name="profile")
    def profile_cmd(
        learner: str | None = typer.Option(None, "--learner", help="学习者 ID（默认当前角色）"),
        grade_subject: str = typer.Option("小学奥数", "--grade-subject", help="学段学科"),
        db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
        config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
        json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
    ) -> None:
        """重放并输出学习者分模块 Elo、总评水位、8 档阶梯与薄弱雷达。"""

        def _work() -> dict[str, Any]:
            settings = load_settings(config_dir / "settings.yaml")
            conn = get_connection(db, readonly=True)
            try:
                lid = facade.resolve_learner_id(conn, learner)
                return facade.compute_learner_profile(
                    conn,
                    learner_id=lid,
                    grade_subject=grade_subject,
                    elo_cfg=settings.learner_elo,
                )
            finally:
                conn.close()

        run_cli_action("profile", json, _work)

    @learner_app.command(name="list")
    def learner_list_cmd(
        db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
        config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
        json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
    ) -> None:
        """列出全部学习者角色。"""

        def _work() -> dict[str, Any]:
            conn = get_connection(db, readonly=True)
            try:
                items = facade.list_learners(conn)
                return {"count": len(items), "learners": items}
            finally:
                conn.close()

        run_cli_action("learner.list", json, _work)

    @learner_app.command(name="create")
    def learner_create_cmd(
        learner_id: str = typer.Argument(..., help="新角色 ID，如 xiaoming"),
        name: str = typer.Option("", "--name", help="显示名称"),
        subject: str = typer.Option("小学奥数", "--subject", help="默认学段学科"),
        current: bool = typer.Option(False, "--current", help="创建并设为当前激活角色"),
        notes: str = typer.Option("", "--notes", help="备注"),
        db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
        config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
        human_dir: Path = typer.Option(Path("human"), "--human-dir", help="人工镜像目录"),
        json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
    ) -> None:
        """创建新学习者角色。"""

        def _work() -> dict[str, Any]:
            settings = load_settings(config_dir / "settings.yaml")
            conn = get_connection(db)
            try:
                init_db(conn, config_dir=config_dir)
                return facade.create_learner(
                    conn,
                    learner_id=learner_id,
                    display_name=name or learner_id,
                    default_grade_subject=subject,
                    set_current=current,
                    notes=notes,
                    human_dir=human_dir,
                    include_practice_log=settings.human_mirror.include_practice_log,
                )
            finally:
                conn.close()

        run_cli_action("learner.create", json, _work)

    @learner_app.command(name="use")
    def learner_use_cmd(
        learner_id: str = typer.Argument(..., help="目标角色 ID"),
        db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
        config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
        human_dir: Path = typer.Option(Path("human"), "--human-dir", help="人工镜像目录"),
        json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
    ) -> None:
        """切换当前激活学习者角色。"""

        def _work() -> dict[str, Any]:
            settings = load_settings(config_dir / "settings.yaml")
            conn = get_connection(db)
            try:
                init_db(conn, config_dir=config_dir)
                return facade.use_learner(
                    conn,
                    learner_id,
                    human_dir=human_dir,
                    include_practice_log=settings.human_mirror.include_practice_log,
                )
            finally:
                conn.close()

        run_cli_action("learner.use", json, _work)

    # 11. shuati render-sheet
    @app.command(name="render-sheet")
    def render_sheet_cmd(
        quiz_batch_id: str | None = typer.Option(None, "--quiz-batch-id", help="按组卷批次 ID 渲染"),
        uids: str | None = typer.Option(None, "--uids", help="逗号分隔题目 UID/简写列表"),
        title: str = typer.Option("智学练习单", "--title", help="练习单标题"),
        embed_images: bool = typer.Option(True, "--embed-images/--no-embed-images", help="是否 Base64 内联图片"),
        print_answers_at_end: bool = typer.Option(True, "--print-answers-at-end/--no-print-answers-at-end", help="打印版末页集中附参考答案"),
        out: Path | None = typer.Option(None, "--out", help="指定输出 HTML 文件路径"),
        sheets_dir: Path = typer.Option(Path("data/sheets"), "--sheets-dir", help="默认练习单输出目录"),
        assets_dir: Path = typer.Option(Path("data/assets/images"), "--assets-dir", help="CAS 图片目录"),
        db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
        json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
    ) -> None:
        """将小卷或题单渲染为离线单文件双模式静态 HTML（屏幕折叠 + 打印留白）。"""

        def _work() -> dict[str, Any]:
            uid_list = [u.strip() for u in uids.split(",") if u.strip()] if uids else None
            if not quiz_batch_id and not uid_list:
                raise PreconditionError("必须提供 --quiz-batch-id 或 --uids")
            conn = get_connection(db, readonly=not bool(quiz_batch_id))
            try:
                return facade.render_practice_sheet(
                    conn,
                    quiz_batch_id=quiz_batch_id,
                    question_uids=uid_list,
                    assets_dir=assets_dir,
                    sheets_dir=sheets_dir,
                    out_path=out,
                    title=title,
                    embed_images=embed_images,
                    print_answers_at_end=print_answers_at_end,
                )
            finally:
                conn.close()

        run_cli_action("render-sheet", json, _work)

    # 12. shuati override <uid>
    @app.command(name="override")
    def override_cmd(
        uid: str = typer.Argument(..., help="题目 question_uid 或 source_id"),
        field: str = typer.Option(..., "--field", help="要覆写的字段名"),
        value: str | None = typer.Option(None, "--value", help="新覆写值"),
        reason: str = typer.Option("", "--reason", help="覆写理由"),
        revoke: bool = typer.Option(False, "--revoke", help="撤回当前字段激活覆写"),
        reconfirm: bool = typer.Option(False, "--reconfirm", help="重新确认源题微调后被挂起的覆写"),
        dry_run: bool = typer.Option(False, "--dry-run", help="仅预览变更，不写库"),
        source: str = typer.Option("PI_CLI", "--source", help="覆写来源标记"),
        db: Path = typer.Option(Path("data/shuati.db"), "--db", help="SQLite 数据库文件路径"),
        config_dir: Path = typer.Option(Path("config"), "--config-dir", help="配置文件目录"),
        human_dir: Path = typer.Option(Path("human"), "--human-dir", help="人工镜像目录"),
        json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
    ) -> None:
        """单字段人工覆写写入、撤回或重确认（支持 --dry-run）。"""

        def _work() -> dict[str, Any]:
            settings = load_settings(config_dir / "settings.yaml")
            conn = get_connection(db)
            try:
                init_db(conn, config_dir=config_dir)
                effective.install_eff_write_guard(conn)
                return facade.apply_single_override(
                    conn,
                    uid,
                    field_name=field,
                    value=value,
                    revoke=revoke,
                    reconfirm=reconfirm,
                    reason=reason,
                    source=source,
                    dry_run=dry_run,
                    human_dir=human_dir,
                    include_practice_log=settings.human_mirror.include_practice_log,
                )
            finally:
                conn.close()

        run_cli_action("override", json, _work)

    # 13. shuati skill install
    @skill_app.command(name="install")
    def skill_install_cmd(
        skill: str | None = typer.Option(
            None,
            "--skill",
            help="要安装的 Skill 名称：all / shuati / shuati-init / shuati-compiler",
        ),
        all_skills: bool = typer.Option(
            False,
            "--all",
            help="一键安装全部内置 Skill（shuati / shuati-init / shuati-compiler）",
        ),
        target: Path | None = typer.Option(
            None,
            "--target",
            help="单 Skill 安装目标路径（兼容旧用法，默认 .pi/skills/shuati）",
        ),
        target_dir: Path = typer.Option(
            Path(".pi/skills"),
            "--target-dir",
            help="多 Skill 安装根目录（默认 .pi/skills）",
        ),
        source: Path | None = typer.Option(
            None,
            "--source",
            help="权威 Skill 源目录（兼容单 Skill 用法，默认 skills/shuati）",
        ),
        source_dir: Path = typer.Option(
            Path("skills"),
            "--source-dir",
            help="内置 Skill 源根目录（默认 skills）",
        ),
        symlink: bool = typer.Option(
            True, "--symlink/--copy", help="优先创建目录软链（Windows 无权限时自动降级复制）"
        ),
        json: bool = typer.Option(False, "--json", help="输出 JSON 格式信封"),
    ) -> None:
        """将内置 Skill（shuati / shuati-init / shuati-compiler）软链或同步安装到 pi 自动发现目录。"""
        builtin_skills = ("shuati", "shuati-init", "shuati-compiler")

        def _install_one(src_path: Path, dst_path: Path) -> dict[str, Any]:
            src_abs = src_path.resolve()
            if not src_abs.exists():
                raise PreconditionError(f"Skill 源目录不存在: {src_abs}")
            dst_abs = dst_path.resolve()
            dst_abs.parent.mkdir(parents=True, exist_ok=True)
            if dst_abs.is_symlink() or dst_abs.is_file():
                dst_abs.unlink()
            elif dst_abs.exists():
                shutil.rmtree(dst_abs)

            installed_mode = "copy"
            if symlink:
                try:
                    os.symlink(src_abs, dst_abs, target_is_directory=True)
                    installed_mode = "symlink"
                except OSError:
                    shutil.copytree(src_abs, dst_abs)
                    installed_mode = "copy_fallback"
            else:
                shutil.copytree(src_abs, dst_abs)

            return {
                "source": str(src_abs),
                "target": str(dst_abs),
                "mode": installed_mode,
                "skill_md_exists": (dst_abs / "SKILL.md").exists(),
            }

        def _work() -> dict[str, Any]:
            if all_skills and skill is not None and skill.strip().lower() != "all":
                raise PreconditionError("--all 与指定单个 --skill 不能冲突传参")

            # 旧调用方式 100% 向后兼容：未传 --all / --skill
            if not all_skills and skill is None:
                src_p = source if source is not None else (source_dir / "shuati")
                dst_p = target if target is not None else (target_dir / "shuati")
                return _install_one(src_p, dst_p)

            if all_skills or (skill is not None and skill.strip().lower() == "all"):
                if target is not None:
                    raise PreconditionError("--all / --skill all 请使用 --target-dir 指定根目录，而非 --target")
                installed: list[dict[str, Any]] = []
                for sk_name in builtin_skills:
                    info = _install_one(source_dir / sk_name, target_dir / sk_name)
                    installed.append({"skill": sk_name, **info})
                return {
                    "target_dir": str(target_dir.resolve()),
                    "count": len(installed),
                    "installed": installed,
                    "all_skill_md_exists": all(it["skill_md_exists"] for it in installed),
                }

            assert skill is not None
            sk_norm = skill.strip()
            if sk_norm not in builtin_skills:
                raise PreconditionError(
                    f"不支持的 --skill: {skill!r}（仅支持 all / {' / '.join(builtin_skills)}）"
                )
            src_p = source if source is not None else (source_dir / sk_norm)
            dst_p = target if target is not None else (target_dir / sk_norm)
            info = _install_one(src_p, dst_p)
            return {
                "skill": sk_norm,
                "count": 1,
                "installed": [{"skill": sk_norm, **info}],
                **info,
            }

        run_cli_action("skill.install", json, _work)
