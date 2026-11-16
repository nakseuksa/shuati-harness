"""SQLite Schema 基座：9 张核心表 + 2 张 PM 批准扩展表与结构化索引的幂等初始化。

DDL 逐字段照抄设计文档 docs/designs/adaptive_practice_harness.md §2（唯一权威规格），
仅按契约将 CREATE TABLE 改为 CREATE TABLE IF NOT EXISTS 以保证幂等。
路径配置一律取自 backend.config.settings。

P10-A（docs/tasks/P10-A_grade_schema_settings.md）：新增第 11 张表 grade_subjects
（学段学科条目），并为 knowledge_nodes / elo_mutation_ledger / compaction_audit_ledger /
concept_co_errors 四表幂等加列 grade_subject + 回填（_migrate_grade_scoping）。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from backend.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 表 DDL（设计 §2.1 ~ §2.4，按文档顺序；SQLite 允许外键前向引用建表）
# ---------------------------------------------------------------------------

TABLE_DDLS: dict[str, str] = {
    "questions": """
CREATE TABLE IF NOT EXISTS questions (
    question_uid TEXT PRIMARY KEY,               -- 幂等主键 (UUIDv5 或 SHA-256 前缀)
    origin_type TEXT NOT NULL CHECK (
        origin_type IN ('USER_CANONICAL', 'AI_SYNTHETIC_SCAFFOLD', 'AI_SYNTHETIC_VARIANT')
    ),
    parent_question_uid TEXT REFERENCES questions(question_uid),
    target_grade_and_subject TEXT NOT NULL,      -- 如 '高中物理-高二'、'高中数学-高三'
    stem_md TEXT NOT NULL,                       -- 题干 Markdown (图片已转为 cas://sha256 URI)
    stem_hash TEXT NOT NULL,                     -- 题干内容 SHA-256 (用于二次导入 Diff)
    standard_answer_latex TEXT NOT NULL,         -- 标准最终答案 LaTeX / SymPy 可解析表达式
    c_score INTEGER NOT NULL CHECK (c_score BETWEEN 1 AND 4),
    s_score INTEGER NOT NULL CHECK (s_score BETWEEN 1 AND 3),
    t_score INTEGER NOT NULL CHECK (t_score BETWEEN 1 AND 3),
    l_score INTEGER NOT NULL CHECK (l_score BETWEEN 1 AND 3),
    base_elo INTEGER NOT NULL CHECK (base_elo BETWEEN 800 AND 2800),
    tier TEXT NOT NULL,                          -- 'L1_基础直达' .. 'L5-3_殿堂极限'
    trap_tags_json TEXT NOT NULL DEFAULT '[]',   -- 命中的数理六大设错算子标签数组
    primary_l1_module TEXT NOT NULL,             -- 归属主 L1 模块名称
    mastery_state TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (
        mastery_state IN ('ACTIVE', 'PENDING_RETEST', 'MASTERED')
    ),
    blind_test_unlock_at DATETIME,               -- 延迟盲测最早解锁时间 (+3d 或 +2d)
    blind_test_due_at DATETIME,                  -- 延迟盲测目标截止时间 (+7d 或 +4d)
    -- 因每日配额上限被系统顺延的天数（顺延期间冻结衰减）
    system_deferred_days INTEGER NOT NULL DEFAULT 0,
    -- 当日已锁定推送盲测题单的日期（实现当日刷新幂等锁）
    slated_drill_date DATE,
    slated_exposure_count INTEGER NOT NULL DEFAULT 0, -- 已被排入每日题单的总曝光天数
    verification_status TEXT NOT NULL DEFAULT 'HUMAN_OR_CANONICAL' CHECK (
        verification_status IN ('HUMAN_OR_CANONICAL', 'SYMPY_VERIFIED', 'DUAL_LLM_FALLBACK')
    ),
    verification_artifact_json TEXT,             -- 保存 SymPy 脚本源码、标准解快照与版本号
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
""",
    "question_steps": """
-- 步骤级结构化表：支持同一道题存在 1..N 条合法解法分支 (solution_branch_id)
CREATE TABLE IF NOT EXISTS question_steps (
    step_id TEXT PRIMARY KEY,
    question_uid TEXT NOT NULL REFERENCES questions(question_uid) ON DELETE CASCADE,
    solution_branch_id TEXT NOT NULL DEFAULT 'canonical', -- 'canonical' 或用户一题多解分支 ID
    step_index INTEGER NOT NULL,                 -- 1-based 步骤序号
    step_goal_desc TEXT NOT NULL,                -- 该步骤子目标描述
    step_formula_latex TEXT NOT NULL,            -- 该步核心方程或转化表达式
    knowledge_node_id TEXT NOT NULL REFERENCES knowledge_nodes(node_id),
    masked_conditions_json TEXT NOT NULL,        -- 一阶盲探需高亮的局部已知条件数组
    probe_question_md TEXT NOT NULL,             -- 一阶盲探反问句（隐去最终问题）
    -- 该步骤是否属于思路停滞/阻断步骤 (step_index >= stalled_step_index)
    is_blocked_step INTEGER NOT NULL DEFAULT 0,
    reused_existing_node INTEGER NOT NULL DEFAULT 0, -- 录入时是否命中了既有 L3 节点 (未长出新节点)
    c_sub INTEGER NOT NULL CHECK (c_sub BETWEEN 1 AND 4),
    s_sub INTEGER NOT NULL CHECK (s_sub BETWEEN 1 AND 3),
    t_sub INTEGER NOT NULL CHECK (t_sub BETWEEN 1 AND 3),
    l_sub INTEGER NOT NULL CHECK (l_sub BETWEEN 1 AND 3),
    sub_elo INTEGER NOT NULL CHECK (sub_elo BETWEEN 800 AND 2650), -- 完成至该步的前缀子问题保底 Elo
    UNIQUE(question_uid, solution_branch_id, step_index)
);
""",
    "question_relations": """
CREATE TABLE IF NOT EXISTS question_relations (
    source_question_uid TEXT NOT NULL REFERENCES questions(question_uid) ON DELETE CASCADE,
    target_question_uid TEXT NOT NULL REFERENCES questions(question_uid) ON DELETE CASCADE,
    relation_type TEXT NOT NULL CHECK (
        relation_type IN ('CLONE', 'ISOMORPHIC', 'SCAFFOLD_PRE', 'CONTRASTIVE', 'SHARED_TRAP')
    ),
    -- SCAFFOLD_PRE 为 1 (source 为前置微母题, target 为原题)
    is_directed INTEGER NOT NULL DEFAULT 0,
    target_step_index INTEGER,                   -- 针对原题哪一步的铺垫或变式
    surface_sim REAL NOT NULL,
    skeleton_sim REAL NOT NULL,
    containment_score REAL NOT NULL,
    trap_overlap REAL NOT NULL,
    confidence_source TEXT NOT NULL CHECK (
        confidence_source IN ('LLM_PRIOR', 'SYMPY_KERNEL_GEN', 'HUMAN_VERIFIED', 'LOG_CALIBRATED')
    ),
    PRIMARY KEY (source_question_uid, target_question_uid, relation_type)
);
""",
    "knowledge_nodes": """
CREATE TABLE IF NOT EXISTS knowledge_nodes (
    node_id TEXT PRIMARY KEY,
    l1_module TEXT NOT NULL,                     -- 第一层：学科模块（如 '电磁学'、'解析几何'）
    -- 第二层：核心实体与定理（如 '带电粒子在有界磁场中运动'）
    l2_entity TEXT NOT NULL,
    -- 第三层：原子转化算子（如 '放缩圆与临界相切构造'）
    l3_operator TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',     -- 同义别称 JSON 数组（上限 8 个）
    redirect_to_id TEXT REFERENCES knowledge_nodes(node_id), -- 并查集父指针（NULL 代表活跃主节点）
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
""",
    "concept_co_errors": """
-- 跨知识点组合错题标红边（仅存储经 CanonicalNodeResolver 解析后的活跃主节点 ID 对，
-- 且强制 node_u_id < node_v_id）
CREATE TABLE IF NOT EXISTS concept_co_errors (
    node_u_id TEXT NOT NULL REFERENCES knowledge_nodes(node_id),
    node_v_id TEXT NOT NULL REFERENCES knowledge_nodes(node_id),
    l1_module_u TEXT NOT NULL,
    l1_module_v TEXT NOT NULL,
    active_error_count INTEGER NOT NULL DEFAULT 1,
    total_historical_count INTEGER NOT NULL DEFAULT 1,
    source_question_uids_json TEXT NOT NULL,
    last_error_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (node_u_id, node_v_id),
    CHECK (node_u_id < node_v_id)                -- 数据库约束级拦截自环边 (u == v) 与无向重复边
);
""",
    "compaction_audit_ledger": """
-- 并查集 Compaction 不可变审计与一键回滚表
CREATE TABLE IF NOT EXISTS compaction_audit_ledger (
    merge_id INTEGER PRIMARY KEY AUTOINCREMENT,  -- 唯一合并流水号
    batch_id TEXT NOT NULL,                      -- 批次 UUID (单批次可记录多对合并)
    l1_module TEXT NOT NULL,
    l2_entity TEXT NOT NULL,
    from_node_id TEXT NOT NULL REFERENCES knowledge_nodes(node_id),
    to_node_id TEXT NOT NULL REFERENCES knowledge_nodes(node_id),
    ai_judge_evidence TEXT NOT NULL,
    previous_aliases_to_json TEXT NOT NULL,      -- 合并前主节点的 aliases 快照
    -- 原指向 from_node_id 的二级子节点快照（用于解绑时完整回滚两跳指针）
    affected_child_node_ids_json TEXT NOT NULL DEFAULT '[]',
    previous_co_errors_snapshot_json TEXT NOT NULL, -- 受影响标红边的合并前快照
    is_reverted INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reverted_at DATETIME
);
""",
    "diagnostic_sessions": """
CREATE TABLE IF NOT EXISTS diagnostic_sessions (
    session_id TEXT PRIMARY KEY,
    question_uid TEXT NOT NULL REFERENCES questions(question_uid) ON DELETE CASCADE,
    draft_image_cas_uris_json TEXT NOT NULL,     -- 原始手写草稿图片 CAS URI 列表
    draft_steps_latex_json TEXT NOT NULL,        -- VLM 一次性转录出的有序手写公式数组
    matched_branch_id TEXT NOT NULL DEFAULT 'canonical',
    -- 已独立完成的最后一步序号 (0 表示一笔未动或第1步即卡壳)
    last_valid_step_index INTEGER NOT NULL DEFAULT 0,
    stalled_step_index INTEGER NOT NULL DEFAULT 1,    -- 发生停滞或错误的步骤序号
    error_category TEXT NOT NULL CHECK (
        error_category IN ('ALREADY_MASTERED', 'TRAP_OR_CALC_ERROR', 'CONCEPT_BOTTLENECK')
    ),
    trap_taxonomy_tag TEXT,                      -- 命中的数理六大易错算子枚举
    user_confirmed_gate INTEGER NOT NULL DEFAULT 0, -- 是否已通过步骤对齐微确认门禁
    user_overridden INTEGER NOT NULL DEFAULT 0,  -- 用户是否手动修正了 VLM 诊断结论
    micro_stage TEXT NOT NULL DEFAULT 'STAGE_0_CONFIRM_GATE' CHECK (
        micro_stage IN (
            'STAGE_0_CONFIRM_GATE',
            'STAGE_1_BLIND_PROBE',
            'STAGE_2_MICRO_SCAFFOLD',
            'STAGE_3_REDO_ORIGINAL',
            'STAGE_4_DECRUTCH_VARIANT',
            'STAGE_COOLDOWN_DEFERRED',
            'SESSION_COMPLETED'
        )
    ),
    prewarm_scaffold_uid TEXT REFERENCES questions(question_uid),
    prewarm_variant_uid TEXT REFERENCES questions(question_uid),
    prewarm_status TEXT NOT NULL DEFAULT 'IDLE' CHECK (
        prewarm_status IN ('IDLE', 'RUNNING', 'READY_SYMPY', 'READY_FALLBACK', 'FAILED')
    ),
    prewarm_excluded_uids_json TEXT NOT NULL DEFAULT '[]',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
""",
    "question_escrow_pool": """
-- 单题实战分差额托管池（防止多阶段重复加分与过度惩罚）
CREATE TABLE IF NOT EXISTS question_escrow_pool (
    question_uid TEXT PRIMARY KEY REFERENCES questions(question_uid) ON DELETE CASCADE,
    l1_module TEXT NOT NULL,
    -- 入库时该模块初始实战分 R_combat^(0) 快照 (用于严格守恒核算)
    initial_combat_elo_snapshot REAL NOT NULL,
    floor_elo_applied REAL NOT NULL DEFAULT 0.0, -- 模块 1 已结算的草稿子步保底实战分增量
    escrow_combat_delta REAL NOT NULL DEFAULT 0.0, -- 模块 3 冻结待盲测兑现的实战分差额
    is_cov_settled INTEGER NOT NULL DEFAULT 0,   -- 当场拆拐杖覆盖分是否已结算 (防重复发放)
    decay_factor REAL NOT NULL DEFAULT 1.0,      -- 逾期未测衰减系数 (1.0 -> 0.0)
    escrow_status TEXT NOT NULL CHECK (
        escrow_status IN (
            'FLOOR_ONLY', 'FROZEN_PENDING_BLIND', 'REALIZED_MASTERED', 'FORFEITED_RESET'
        )
    ),
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
""",
    "elo_mutation_ledger": """
-- 分模块双轨 Elo 不可变事件溯源账本
CREATE TABLE IF NOT EXISTS elo_mutation_ledger (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 格式: '{question_uid}:{session_id}:{mutation_trigger}'
    idempotency_key TEXT NOT NULL UNIQUE,
    l1_module TEXT NOT NULL,
    question_uid TEXT NOT NULL REFERENCES questions(question_uid),
    mutation_trigger TEXT NOT NULL CHECK (
        mutation_trigger IN (
            'MASTERED_INIT',
            'PARTIAL_DRAFT_SUBGOAL_FLOOR',
            'SCAFFOLD_COVER_GAIN',
            'BLIND_TEST_REALIZE',
            'ESCROW_TIMEOUT_DECAY'
        )
    ),
    eval_difficulty_elo INTEGER NOT NULL,        -- 参与本次结算的基准难度 (D_orig 或 D_sub)
    watermark_gamma REAL NOT NULL DEFAULT 1.0,   -- 水位线高斯门控系数
    coverage_elo_before REAL NOT NULL,
    coverage_elo_after REAL NOT NULL,
    combat_elo_before REAL NOT NULL,
    combat_elo_after REAL NOT NULL,
    explanation_text TEXT NOT NULL,              -- 面向前端展示的自然语言算分解释条
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
""",
    # PM 批准扩展（P3-C），见 docs/tasks/P3-C_blind_drill.md：
    # 变式曝光追踪（"未曝光过"判据）+ 失败重训分流的作答日志。
    "blind_drill_attempts": """
CREATE TABLE IF NOT EXISTS blind_drill_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_uid TEXT NOT NULL REFERENCES questions(question_uid) ON DELETE CASCADE,
    drill_question_uid TEXT NOT NULL REFERENCES questions(question_uid),
    active_date DATE NOT NULL,
    intermediate_correct INTEGER,          -- NULL = 本题不要求中间量
    final_correct INTEGER NOT NULL,
    passed INTEGER NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
""",
    # PM 批准扩展（P10-A），见 docs/tasks/P10-A_grade_schema_settings.md：
    # 学段学科条目表（自由单字符串，如 '小学奥数'、'高中数学-高一'）。
    "grade_subjects": """
CREATE TABLE IF NOT EXISTS grade_subjects (
    grade_subject TEXT PRIMARY KEY,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
""",
}

# 表名清单：9 张核心表 + 2 张 PM 批准扩展表 blind_drill_attempts / grade_subjects
# （与 DDL 定义顺序一致）
TABLE_NAMES: tuple[str, ...] = tuple(TABLE_DDLS)

# ---------------------------------------------------------------------------
# 结构化索引（P1-A 契约要求的最小覆盖集，全部 IF NOT EXISTS 幂等）
# ---------------------------------------------------------------------------

INDEX_DDLS: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_questions_mastery_state ON questions(mastery_state);",
    "CREATE INDEX IF NOT EXISTS idx_questions_primary_l1_module ON questions(primary_l1_module);",
    "CREATE INDEX IF NOT EXISTS idx_questions_blind_test_unlock_at"
    " ON questions(blind_test_unlock_at);",
    "CREATE INDEX IF NOT EXISTS idx_questions_slated_drill_date ON questions(slated_drill_date);",
    "CREATE INDEX IF NOT EXISTS idx_question_steps_question_uid ON question_steps(question_uid);",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_l1_l2"
    " ON knowledge_nodes(l1_module, l2_entity);",
    "CREATE INDEX IF NOT EXISTS idx_diagnostic_sessions_question_uid"
    " ON diagnostic_sessions(question_uid);",
    "CREATE INDEX IF NOT EXISTS idx_elo_mutation_ledger_l1_module"
    " ON elo_mutation_ledger(l1_module);",
    "CREATE INDEX IF NOT EXISTS idx_question_relations_target_type"
    " ON question_relations(target_question_uid, relation_type);",
    # PM 批准扩展（P3-C）：变式曝光追踪按 (原题, 活跃日) 检索
    "CREATE INDEX IF NOT EXISTS idx_blind_drill_attempts_question_date"
    " ON blind_drill_attempts(question_uid, active_date);",
    # P10-A 学段学科分区索引（grade_subject 列由 _migrate_grade_scoping 在建索引前补齐）
    "CREATE INDEX IF NOT EXISTS idx_questions_target_grade_and_subject"
    " ON questions(target_grade_and_subject);",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_grade_l1_l2"
    " ON knowledge_nodes(grade_subject, l1_module, l2_entity);",
    "CREATE INDEX IF NOT EXISTS idx_elo_mutation_ledger_grade_l1"
    " ON elo_mutation_ledger(grade_subject, l1_module);",
    "CREATE INDEX IF NOT EXISTS idx_concept_co_errors_grade ON concept_co_errors(grade_subject);",
    "CREATE INDEX IF NOT EXISTS idx_compaction_audit_ledger_grade"
    " ON compaction_audit_ledger(grade_subject);",
)

# P10-A：需要加列回填 grade_subject 的四张分区表
# （questions 自带 target_grade_and_subject，无需加列）。
_GRADE_SCOPED_TABLES: tuple[str, ...] = (
    "knowledge_nodes",
    "elo_mutation_ledger",
    "compaction_audit_ledger",
    "concept_co_errors",
)


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """PRAGMA table_info 检测表是否已有指定列（表名仅来自模块内常量）。"""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def _migrate_grade_scoping(conn: sqlite3.Connection) -> None:
    """P10-A 学段学科分区迁移：四表加 grade_subject 列并回填（幂等、纯增量非破坏）。

    规则（契约 docs/tasks/P10-A_grade_schema_settings.md §修改方案 1.3）：
    - 仅回填 grade_subject = '' 的行，重复调用零变化；不删改任何既有数据值；
    - knowledge_nodes 经 question_steps JOIN questions 取挂载题的 DISTINCT 学段；
      同一节点出现多个不同学段 → ValueError（数据违约，绝不静默择一，先全量校验再写入）；
    - 无自身步骤的 redirect 子节点继承其 redirect 目标的学段（迭代至不动点，
      兼容多跳链；目标学段为 '' 时保持 ''）；
    - elo_mutation_ledger 按 question_uid、compaction_audit_ledger 按 from_node_id
      （节点回填之后）、concept_co_errors 按 node_u_id 回填；
    - 无步骤且无目标学段的节点保持 ''（计数告警汇报，真实数据不应存在）；
    - questions 的 DISTINCT 学段自动注册为 grade_subjects 条目（INSERT OR IGNORE）。

    调用方管理事务（init_db 同一连接）；违约抛错时由连接上下文整体回滚。
    """
    for table in _GRADE_SCOPED_TABLES:
        if not _has_column(conn, table, "grade_subject"):
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN grade_subject TEXT NOT NULL DEFAULT ''"
            )

    # 1) knowledge_nodes：先全量收集 '' 节点的挂载题学段并校验跨学段违约，再回填。
    rows = conn.execute(
        """
        SELECT DISTINCT s.knowledge_node_id AS node_id, q.target_grade_and_subject AS grade
        FROM question_steps s
        JOIN questions q ON q.question_uid = s.question_uid
        JOIN knowledge_nodes kn ON kn.node_id = s.knowledge_node_id
        WHERE kn.grade_subject = ''
        ORDER BY s.knowledge_node_id, q.target_grade_and_subject
        """
    ).fetchall()
    node_grades: dict[str, list[str]] = {}
    for row in rows:
        node_grades.setdefault(row["node_id"], []).append(row["grade"])
    conflicts = {node: grades for node, grades in node_grades.items() if len(grades) > 1}
    if conflicts:
        detail = "; ".join(f"{node} -> {grades}" for node, grades in sorted(conflicts.items()))
        raise ValueError(f"知识节点跨学段共享（数据违约，拒绝回填）: {detail}")
    for node_id, grades in node_grades.items():
        conn.execute(
            "UPDATE knowledge_nodes SET grade_subject = ?"
            " WHERE node_id = ? AND grade_subject = ''",
            (grades[0], node_id),
        )

    # 2) 无自身步骤的 redirect 子节点继承目标学段（迭代至不动点；目标为 '' 则保持 ''）。
    while True:
        cur = conn.execute(
            """
            UPDATE knowledge_nodes
            SET grade_subject = (
                SELECT t.grade_subject FROM knowledge_nodes t
                WHERE t.node_id = knowledge_nodes.redirect_to_id
            )
            WHERE grade_subject = ''
              AND redirect_to_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM question_steps s
                  WHERE s.knowledge_node_id = knowledge_nodes.node_id
              )
              AND COALESCE((
                  SELECT t.grade_subject FROM knowledge_nodes t
                  WHERE t.node_id = knowledge_nodes.redirect_to_id
              ), '') <> ''
            """
        )
        if cur.rowcount == 0:
            break

    # 3) elo_mutation_ledger：按 question_uid JOIN questions 回填。
    conn.execute(
        """
        UPDATE elo_mutation_ledger
        SET grade_subject = (
            SELECT q.target_grade_and_subject FROM questions q
            WHERE q.question_uid = elo_mutation_ledger.question_uid
        )
        WHERE grade_subject = ''
        """
    )

    # 4) compaction_audit_ledger：按 from_node_id 回填（节点回填之后执行）。
    conn.execute(
        """
        UPDATE compaction_audit_ledger
        SET grade_subject = (
            SELECT kn.grade_subject FROM knowledge_nodes kn
            WHERE kn.node_id = compaction_audit_ledger.from_node_id
        )
        WHERE grade_subject = ''
        """
    )

    # 5) concept_co_errors：按 node_u_id 回填。
    conn.execute(
        """
        UPDATE concept_co_errors
        SET grade_subject = (
            SELECT kn.grade_subject FROM knowledge_nodes kn
            WHERE kn.node_id = concept_co_errors.node_u_id
        )
        WHERE grade_subject = ''
        """
    )

    # 6) 现有学段自动注册为条目（DISTINCT 幂等；空串学段不构成合法条目，不注册）。
    conn.execute(
        """
        INSERT OR IGNORE INTO grade_subjects (grade_subject, created_at)
        SELECT DISTINCT target_grade_and_subject, CURRENT_TIMESTAMP
        FROM questions
        WHERE target_grade_and_subject <> ''
        """
    )

    # 7) 无学段来源的节点保持 ''：计数汇报（真实数据不应存在）。
    orphan_row = conn.execute(
        "SELECT COUNT(*) AS n FROM knowledge_nodes WHERE grade_subject = ''"
    ).fetchone()
    orphan_count = int(orphan_row["n"])
    if orphan_count:
        logger.warning(
            "P10-A 迁移：%d 个知识节点无挂载步骤且无 redirect 目标学段，grade_subject 保持 ''",
            orphan_count,
        )


def init_db(db_path: Path | None = None) -> None:
    """幂等初始化数据库：自动创建父目录并建立 9 张核心表 + 2 张 PM 批准扩展表与全部索引。

    db_path 缺省取 backend.config.settings.SQLITE_PATH；重复调用不报错、不丢数据。
    建表后在同一连接内执行 P10-A 学段分区迁移（加列回填，幂等），再建索引
    （新索引引用 grade_subject 列，必须先完成加列）。
    """
    path = db_path if db_path is not None else settings.SQLITE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with get_db_connection(path) as conn:
        for ddl in TABLE_DDLS.values():
            conn.execute(ddl)
        if not _has_column(conn, "diagnostic_sessions", "prewarm_excluded_uids_json"):
            conn.execute(
                "ALTER TABLE diagnostic_sessions ADD COLUMN prewarm_excluded_uids_json "
                "TEXT NOT NULL DEFAULT '[]'"
            )
        _migrate_grade_scoping(conn)
        for ddl in INDEX_DDLS:
            conn.execute(ddl)


@contextmanager
def get_db_connection(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """获取强制 WAL / foreign_keys=ON / busy_timeout=5000 的连接上下文管理器。

    row_factory 固定为 sqlite3.Row；正常退出 commit，异常 rollback，最终 close。
    """
    path = db_path if db_path is not None else settings.SQLITE_PATH
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.row_factory = sqlite3.Row
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
