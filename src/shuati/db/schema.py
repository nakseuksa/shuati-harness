"""SQLite DDL（20 张核心表 + 1 张 FTS5 trigram 虚表）与 Bootstrap 逻辑。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import yaml

SCHEMA_VERSION = 1

DDL_STATEMENTS = [
    # 1. 学段学科注册表
    """
    CREATE TABLE IF NOT EXISTS grade_subjects (
        grade_subject TEXT PRIMARY KEY,
        slug TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        subject_family TEXT NOT NULL CHECK (
            subject_family IN ('MATH', 'PHYSICS', 'CHEMISTRY', 'STEM_OTHER')
        ),
        taxonomy_status TEXT NOT NULL DEFAULT 'NONE' CHECK (
            taxonomy_status IN ('NONE', 'DRAFT', 'LOCKED')
        ),
        taxonomy_version TEXT NOT NULL DEFAULT '',
        taxonomy_file TEXT,
        anchors_file TEXT,
        anchors_version TEXT NOT NULL DEFAULT '',
        anchors_ready INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    # 2. 原始题库快照表
    """
    CREATE TABLE IF NOT EXISTS raw_questions (
        question_uid TEXT PRIMARY KEY,
        grade_subject TEXT NOT NULL REFERENCES grade_subjects(grade_subject),
        source_type TEXT NOT NULL CHECK (
            source_type IN ('BATCH_JSONL', 'EXTERNAL_MISTAKE')
        ),
        source_collection TEXT NOT NULL,
        source_file TEXT,
        source_id TEXT NOT NULL,
        raw_chapter TEXT,
        raw_question TEXT NOT NULL,
        raw_images_json TEXT NOT NULL DEFAULT '[]',
        cas_images_json TEXT NOT NULL DEFAULT '[]',
        image_captions_json TEXT NOT NULL DEFAULT '[]',
        legacy_tags_json TEXT NOT NULL DEFAULT '{}',
        content_hash TEXT NOT NULL,
        plain_fingerprint TEXT NOT NULL DEFAULT '',
        duplicate_of_uid TEXT REFERENCES raw_questions(question_uid),
        is_missing_from_source INTEGER NOT NULL DEFAULT 0,
        last_seen_scan_id TEXT NOT NULL DEFAULT '',
        ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(grade_subject, source_collection, source_id)
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_raw_questions_hash ON raw_questions(grade_subject, content_hash);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_raw_questions_plain ON raw_questions(grade_subject, plain_fingerprint);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_raw_questions_dup ON raw_questions(duplicate_of_uid) WHERE duplicate_of_uid IS NOT NULL;
    """,
    # 3. Stage 1 VLM 双盲求解与验真审计表
    """
    CREATE TABLE IF NOT EXISTS stage1_vlm_runs (
        run_id INTEGER PRIMARY KEY AUTOINCREMENT,
        question_uid TEXT NOT NULL REFERENCES raw_questions(question_uid) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK (role IN ('PRIMARY', 'SECONDARY')),
        model_name TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        input_hash TEXT NOT NULL,
        run_status TEXT NOT NULL CHECK (run_status IN ('OK', 'FAILED')),
        error_message TEXT NOT NULL DEFAULT '',
        visual_facts TEXT NOT NULL DEFAULT '',
        visual_ambiguity_flag INTEGER NOT NULL DEFAULT 0,
        visual_ambiguity_note TEXT NOT NULL DEFAULT '',
        method_summary TEXT NOT NULL DEFAULT '',
        solution_markdown TEXT NOT NULL DEFAULT '',
        final_answers_json TEXT NOT NULL DEFAULT '[]',
        sympy_code TEXT,
        sympy_status TEXT NOT NULL DEFAULT 'NOT_RUN' CHECK (
            sympy_status IN ('PASSED', 'PARTIAL', 'FAILED', 'TIMEOUT', 'AST_REJECTED', 'SKIPPED_NO_SYMBOLIC', 'NOT_RUN')
        ),
        sympy_output_json TEXT,
        raw_response TEXT NOT NULL DEFAULT '',
        token_usage_json TEXT NOT NULL DEFAULT '{}',
        latency_ms INTEGER,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_stage1_runs_q ON stage1_vlm_runs(question_uid, role, created_at DESC);
    """,
    # 4. 编译后题目主表
    """
    CREATE TABLE IF NOT EXISTS questions (
        question_uid TEXT PRIMARY KEY REFERENCES raw_questions(question_uid) ON DELETE RESTRICT,
        grade_subject TEXT NOT NULL REFERENCES grade_subjects(grade_subject),
        cas_images_json TEXT NOT NULL DEFAULT '[]',
        image_captions_json TEXT NOT NULL DEFAULT '[]',
        has_images INTEGER NOT NULL DEFAULT 0,
        is_retired INTEGER NOT NULL DEFAULT 0,
        retired_reason TEXT NOT NULL DEFAULT '',

        -- 机器层 m_*
        m_question_text TEXT NOT NULL,
        m_options_json TEXT NOT NULL DEFAULT '{}',
        m_format TEXT NOT NULL CHECK (
            m_format IN ('选择题', '填空题', '判断题', '计算题', '解答题', '证明题', '作图操作题', '实验探究题')
        ),
        m_visual_facts TEXT NOT NULL DEFAULT '',
        m_verification_status TEXT NOT NULL DEFAULT 'PENDING' CHECK (
            m_verification_status IN ('PENDING', 'VERIFIED_CONSENSUS', 'NEEDS_REVIEW')
        ),
        m_final_answers_json TEXT NOT NULL DEFAULT '[]',
        m_abstract_skeleton TEXT NOT NULL DEFAULT '',
        m_primary_l1_module TEXT NOT NULL DEFAULT '',
        m_secondary_l1_modules_json TEXT NOT NULL DEFAULT '[]',
        m_c_score INTEGER CHECK (m_c_score IS NULL OR m_c_score BETWEEN 1 AND 4),
        m_s_score INTEGER CHECK (m_s_score IS NULL OR m_s_score BETWEEN 1 AND 3),
        m_t_score INTEGER CHECK (m_t_score IS NULL OR m_t_score BETWEEN 1 AND 3),
        m_l_score INTEGER CHECK (m_l_score IS NULL OR m_l_score BETWEEN 1 AND 3),

        -- 机器专属列
        review_reason TEXT NOT NULL DEFAULT '',
        part_verdicts_json TEXT NOT NULL DEFAULT '[]',
        raw_l1_candidates_json TEXT NOT NULL DEFAULT '[]',
        raw_l2_candidates_json TEXT NOT NULL DEFAULT '[]',
        raw_thinking_methods_json TEXT NOT NULL DEFAULT '[]',
        raw_classic_motifs_json TEXT NOT NULL DEFAULT '[]',
        raw_trap_tags_json TEXT NOT NULL DEFAULT '[]',
        difficulty_evidence_json TEXT NOT NULL DEFAULT '{}',

        -- 生效层 eff_*
        eff_question_text TEXT NOT NULL DEFAULT '',
        eff_options_json TEXT NOT NULL DEFAULT '{}',
        eff_format TEXT NOT NULL DEFAULT '解答题',
        eff_visual_facts TEXT NOT NULL DEFAULT '',
        eff_verification_status TEXT NOT NULL DEFAULT 'PENDING' CHECK (
            eff_verification_status IN ('PENDING', 'VERIFIED_CONSENSUS', 'NEEDS_REVIEW', 'HUMAN_VERIFIED')
        ),
        eff_review_reason TEXT NOT NULL DEFAULT '',
        eff_verification_mode TEXT NOT NULL DEFAULT 'DETERMINISTIC' CHECK (
            eff_verification_mode IN ('DETERMINISTIC', 'QUALITATIVE', 'MIXED')
        ),
        eff_final_answers_json TEXT NOT NULL DEFAULT '[]',
        eff_abstract_skeleton TEXT NOT NULL DEFAULT '',
        eff_primary_l1_module TEXT NOT NULL DEFAULT '',
        eff_secondary_l1_modules_json TEXT NOT NULL DEFAULT '[]',
        eff_node_ids_json TEXT NOT NULL DEFAULT '[]',
        eff_c_score INTEGER,
        eff_s_score INTEGER,
        eff_t_score INTEGER,
        eff_l_score INTEGER,
        eff_base_elo INTEGER CHECK (eff_base_elo IS NULL OR eff_base_elo BETWEEN 800 AND 2800),
        eff_tier TEXT CHECK (
            eff_tier IS NULL OR eff_tier IN (
                'L1_基础直达', 'L2_常规巩固', 'L3_综合中档',
                'L4-A_进阶拔高(繁)', 'L4-B_进阶拔高(巧)',
                'L5-1_压轴挑战', 'L5-2_竞赛超难', 'L5-3_殿堂极限'
            )
        ),
        eff_tier_rank INTEGER CHECK (eff_tier_rank IS NULL OR eff_tier_rank BETWEEN 1 AND 8),
        eff_is_excluded INTEGER NOT NULL DEFAULT 0,
        eff_is_servable INTEGER NOT NULL DEFAULT 0,
        eff_overridden_fields_json TEXT NOT NULL DEFAULT '[]',
        eff_refreshed_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_q_eff_serve ON questions(grade_subject, eff_is_servable, eff_primary_l1_module, eff_base_elo);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_q_eff_status ON questions(grade_subject, eff_verification_status);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_q_eff_tier ON questions(grade_subject, eff_tier_rank);
    """,
    # 5. 编译状态表
    """
    CREATE TABLE IF NOT EXISTS compile_state (
        question_uid TEXT NOT NULL REFERENCES raw_questions(question_uid) ON DELETE CASCADE,
        stage TEXT NOT NULL CHECK (stage IN ('S1', 'S2', 'S3C', 'S3D', 'S4')),
        status TEXT NOT NULL DEFAULT 'PENDING' CHECK (
            status IN ('PENDING', 'RUNNING', 'OK', 'FAILED', 'STALE', 'BLOCKED')
        ),
        input_hash TEXT NOT NULL DEFAULT '',
        prompt_version TEXT NOT NULL DEFAULT '',
        model_name TEXT NOT NULL DEFAULT '',
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (question_uid, stage)
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_compile_state_status ON compile_state(stage, status);
    """,
    # 6. 一题多解分支表
    """
    CREATE TABLE IF NOT EXISTS question_solutions (
        solution_id TEXT PRIMARY KEY,
        question_uid TEXT NOT NULL REFERENCES questions(question_uid) ON DELETE CASCADE,
        branch_id TEXT NOT NULL,
        is_optimal INTEGER NOT NULL DEFAULT 1,
        source_origin TEXT NOT NULL CHECK (
            source_origin IN ('PRIMARY_VLM', 'SECONDARY_VLM', 'HUMAN', 'KNOWN_ANSWER')
        ),
        origin_run_id INTEGER,
        method_summary TEXT NOT NULL DEFAULT '',
        solution_markdown TEXT NOT NULL,
        sympy_code TEXT,
        sympy_verified INTEGER NOT NULL DEFAULT 0,
        UNIQUE(question_uid, branch_id)
    );
    """,
    # 7. 步骤骨架表
    """
    CREATE TABLE IF NOT EXISTS question_steps (
        step_id TEXT PRIMARY KEY,
        question_uid TEXT NOT NULL REFERENCES questions(question_uid) ON DELETE CASCADE,
        branch_id TEXT NOT NULL DEFAULT 'branch_1',
        step_index INTEGER NOT NULL,
        sub_goal TEXT NOT NULL,
        core_formula TEXT NOT NULL DEFAULT '',
        hint TEXT NOT NULL DEFAULT '',
        raw_l1 TEXT NOT NULL DEFAULT '',
        raw_l2 TEXT NOT NULL DEFAULT '',
        raw_l3 TEXT NOT NULL DEFAULT '',
        m_l1 TEXT NOT NULL DEFAULT '',
        m_l2 TEXT NOT NULL DEFAULT '',
        m_knowledge_node_id TEXT REFERENCES knowledge_nodes(node_id),
        UNIQUE(question_uid, branch_id, step_index),
        FOREIGN KEY (question_uid, branch_id) REFERENCES question_solutions(question_uid, branch_id) ON DELETE CASCADE
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_steps_node ON question_steps(m_knowledge_node_id);
    """,
    # 8. 人工覆盖层表
    """
    CREATE TABLE IF NOT EXISTS human_overrides (
        override_id INTEGER PRIMARY KEY AUTOINCREMENT,
        question_uid TEXT NOT NULL REFERENCES questions(question_uid) ON DELETE RESTRICT,
        field_name TEXT NOT NULL CHECK (
            field_name IN (
                'question_text', 'visual_facts', 'final_answers_json', 'solutions_override_json',
                'difficulty_override_json', 'primary_l1_module', 'secondary_l1_modules_json',
                'taxonomy_override_json', 'thinking_methods_json', 'classic_motifs_json', 'trap_tags_json',
                'abstract_skeleton', 'format', 'verification_status', 'is_excluded'
            )
        ),
        override_value TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL CHECK (source IN ('REVIEW_UI', 'PI_CLI', 'PI_MCP', 'MANUAL_YAML', 'ANCHOR_CALIBRATION')),
        bundle_id TEXT,
        base_fingerprint TEXT NOT NULL,
        taxonomy_version TEXT NOT NULL DEFAULT '',
        is_active INTEGER NOT NULL DEFAULT 1,
        is_suspended INTEGER NOT NULL DEFAULT 0,
        suspended_reason TEXT NOT NULL DEFAULT '',
        revoked_at TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_overrides_active ON human_overrides(question_uid, field_name) WHERE is_active = 1;
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_overrides_history ON human_overrides(question_uid, field_name, created_at DESC);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_overrides_bundle ON human_overrides(bundle_id) WHERE bundle_id IS NOT NULL;
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_overrides_suspended ON human_overrides(is_suspended) WHERE is_active = 1 AND is_suspended = 1;
    """,
    # 9. 三层知识网节点表
    """
    CREATE TABLE IF NOT EXISTS knowledge_nodes (
        node_id TEXT PRIMARY KEY,
        grade_subject TEXT NOT NULL REFERENCES grade_subjects(grade_subject),
        taxonomy_version TEXT NOT NULL,
        node_level TEXT NOT NULL DEFAULT 'L3' CHECK (node_level IN ('L2', 'L3')),
        l1_module TEXT NOT NULL,
        l2_entity TEXT NOT NULL,
        l3_operator TEXT NOT NULL,
        aliases_json TEXT NOT NULL DEFAULT '[]',
        redirect_to_id TEXT REFERENCES knowledge_nodes(node_id),
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        CHECK (
            (node_level = 'L2' AND l3_operator = '' AND redirect_to_id IS NULL)
            OR (node_level = 'L3' AND l3_operator != '')
        )
    );
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_kn_active
        ON knowledge_nodes(grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator)
        WHERE redirect_to_id IS NULL;
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_kn_scope ON knowledge_nodes(grade_subject, node_level, l1_module, l2_entity, redirect_to_id);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_kn_redirect ON knowledge_nodes(redirect_to_id) WHERE redirect_to_id IS NOT NULL;
    """,
    # 10. 知识点拓扑边
    """
    CREATE TABLE IF NOT EXISTS knowledge_edges (
        source_node_id TEXT NOT NULL REFERENCES knowledge_nodes(node_id) ON DELETE CASCADE,
        target_node_id TEXT NOT NULL REFERENCES knowledge_nodes(node_id) ON DELETE CASCADE,
        grade_subject TEXT NOT NULL REFERENCES grade_subjects(grade_subject),
        edge_type TEXT NOT NULL CHECK (edge_type IN ('PREREQUISITE', 'CO_OCCURRENCE')),
        weight REAL NOT NULL DEFAULT 1.0,
        evidence_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (source_node_id, target_node_id, edge_type),
        CHECK (source_node_id != target_node_id)
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_ke_scope ON knowledge_edges(grade_subject, edge_type);
    """,
    # 11. 词表候选
    """
    CREATE TABLE IF NOT EXISTS taxonomy_candidates (
        candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
        grade_subject TEXT NOT NULL REFERENCES grade_subjects(grade_subject),
        base_taxonomy_version TEXT NOT NULL,
        level TEXT NOT NULL CHECK (level IN ('L1', 'L2')),
        parent_l1 TEXT NOT NULL DEFAULT '',
        proposed_name TEXT NOT NULL,
        example_question_uids_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'MAPPED')),
        mapped_to TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        resolved_at TEXT
    );
    """,
    # 12. 归并审计账本
    """
    CREATE TABLE IF NOT EXISTS compaction_audit_ledger (
        merge_id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT NOT NULL,
        target_kind TEXT NOT NULL CHECK (target_kind IN ('KNOWLEDGE_NODE', 'CONCEPT')),
        scope_json TEXT NOT NULL,
        from_id TEXT NOT NULL,
        to_id TEXT NOT NULL,
        ai_judge_evidence TEXT NOT NULL,
        previous_aliases_to_json TEXT NOT NULL,
        affected_child_ids_json TEXT NOT NULL DEFAULT '[]',
        is_reverted INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        reverted_at TEXT
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_compaction_batch ON compaction_audit_ledger(batch_id, is_reverted);
    """,
    # 13. 全局概念注册表
    """
    CREATE TABLE IF NOT EXISTS concepts (
        concept_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('METHOD', 'MOTIF', 'TRAP')),
        name TEXT NOT NULL,
        aliases_json TEXT NOT NULL DEFAULT '[]',
        description TEXT NOT NULL DEFAULT '',
        redirect_to_id TEXT REFERENCES concepts(concept_id),
        is_seed INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_concepts_active ON concepts(kind, name) WHERE redirect_to_id IS NULL;
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_concepts_redirect ON concepts(redirect_to_id) WHERE redirect_to_id IS NOT NULL;
    """,
    # 14. 题目概念挂载
    """
    CREATE TABLE IF NOT EXISTS question_concepts (
        question_uid TEXT NOT NULL REFERENCES questions(question_uid) ON DELETE CASCADE,
        concept_id TEXT NOT NULL REFERENCES concepts(concept_id),
        kind TEXT NOT NULL CHECK (kind IN ('METHOD', 'MOTIF', 'TRAP')),
        origin TEXT NOT NULL CHECK (origin IN ('MACHINE', 'HUMAN')),
        PRIMARY KEY (question_uid, concept_id)
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_qc_concept ON question_concepts(concept_id, kind);
    """,
    # 15. 全库题目五边拓扑表
    """
    CREATE TABLE IF NOT EXISTS question_relations (
        source_question_uid TEXT NOT NULL REFERENCES questions(question_uid) ON DELETE CASCADE,
        target_question_uid TEXT NOT NULL REFERENCES questions(question_uid) ON DELETE CASCADE,
        relation_type TEXT NOT NULL CHECK (
            relation_type IN ('CLONE', 'ISOMORPHIC', 'SCAFFOLD', 'CONTRASTIVE', 'SHARED_TRAP')
        ),
        is_directed INTEGER NOT NULL DEFAULT 0,
        is_cross_scope INTEGER NOT NULL DEFAULT 0,
        surface_sim REAL NOT NULL,
        skeleton_sim REAL NOT NULL,
        surface_pct REAL NOT NULL,
        skeleton_pct REAL NOT NULL,
        containment_score REAL NOT NULL,
        trap_overlap REAL NOT NULL,
        confidence_source TEXT NOT NULL CHECK (
            confidence_source IN ('RULE_MATRIX', 'LLM_VERIFIED', 'HUMAN_VERIFIED')
        ),
        pedagogical_note TEXT NOT NULL DEFAULT '',
        rule_version TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (source_question_uid, target_question_uid, relation_type),
        CHECK (source_question_uid != target_question_uid),
        CHECK ((relation_type = 'SCAFFOLD') = (is_directed = 1)),
        CHECK (is_directed = 1 OR source_question_uid < target_question_uid)
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_qr_source ON question_relations(source_question_uid, relation_type);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_qr_target ON question_relations(target_question_uid, relation_type);
    """,
    # 16. 关系人工裁定表
    """
    CREATE TABLE IF NOT EXISTS relation_overrides (
        override_id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_question_uid TEXT NOT NULL REFERENCES questions(question_uid) ON DELETE RESTRICT,
        target_question_uid TEXT NOT NULL REFERENCES questions(question_uid) ON DELETE RESTRICT,
        relation_type TEXT NOT NULL CHECK (
            relation_type IN ('CLONE', 'ISOMORPHIC', 'SCAFFOLD', 'CONTRASTIVE', 'SHARED_TRAP')
        ),
        action TEXT NOT NULL CHECK (action IN ('ASSERT', 'REJECT')),
        pedagogical_note TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL CHECK (source IN ('REVIEW_UI', 'PI_CLI', 'PI_MCP', 'MANUAL_YAML')),
        is_active INTEGER NOT NULL DEFAULT 1,
        revoked_at TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        CHECK (source_question_uid != target_question_uid),
        CHECK (relation_type = 'SCAFFOLD' OR source_question_uid < target_question_uid)
    );
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_rel_overrides_active
        ON relation_overrides(source_question_uid, target_question_uid, relation_type) WHERE is_active = 1;
    """,
    # 17. 多角色注册表
    """
    CREATE TABLE IF NOT EXISTS learners (
        learner_id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        default_grade_subject TEXT NOT NULL DEFAULT '小学奥数' REFERENCES grade_subjects(grade_subject),
        is_current INTEGER NOT NULL DEFAULT 0,
        notes TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_learners_current ON learners(is_current) WHERE is_current = 1;
    """,
    # 18. 组卷快照表
    """
    CREATE TABLE IF NOT EXISTS quiz_batches (
        quiz_batch_id TEXT PRIMARY KEY,
        learner_id TEXT NOT NULL REFERENCES learners(learner_id) ON DELETE RESTRICT,
        grade_subject TEXT NOT NULL REFERENCES grade_subjects(grade_subject),
        params_json TEXT NOT NULL,
        items_json TEXT NOT NULL,
        sheet_path TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_quiz_learner_time ON quiz_batches(learner_id, created_at DESC);
    """,
    # 19. 做题足迹表
    """
    CREATE TABLE IF NOT EXISTS practice_log (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        learner_id TEXT NOT NULL DEFAULT 'default' REFERENCES learners(learner_id) ON DELETE RESTRICT,
        question_uid TEXT NOT NULL REFERENCES questions(question_uid) ON DELETE RESTRICT,
        practiced_at TEXT NOT NULL DEFAULT (datetime('now')),
        outcome TEXT NOT NULL CHECK (
            outcome IN ('SOLVED_SOLO', 'SOLVED_WITH_HINT', 'WRONG', 'SKIPPED', 'VOIDED')
        ),
        score_ratio REAL CHECK (score_ratio IS NULL OR (score_ratio >= 0.0 AND score_ratio <= 1.0)),
        voids_log_id INTEGER REFERENCES practice_log(log_id),
        stuck_step_index INTEGER,
        stuck_node_id TEXT,
        stuck_sub_goal TEXT,
        hint_level_used INTEGER,
        user_note TEXT NOT NULL DEFAULT '',
        submitted_answer TEXT,
        quiz_batch_id TEXT,
        client_request_id TEXT,
        CHECK ((outcome = 'VOIDED') = (voids_log_id IS NOT NULL)),
        CHECK (outcome != 'SOLVED_SOLO' OR hint_level_used IS NULL OR hint_level_used = 0),
        UNIQUE (learner_id, client_request_id)
    );
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_practice_voids ON practice_log(voids_log_id) WHERE voids_log_id IS NOT NULL;
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_practice_learner_time ON practice_log(learner_id, practiced_at DESC);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_practice_learner_q ON practice_log(learner_id, question_uid, practiced_at DESC);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_practice_batch ON practice_log(quiz_batch_id) WHERE quiz_batch_id IS NOT NULL;
    """,
    # 20. LLM 调用账本
    """
    CREATE TABLE IF NOT EXISTS llm_calls (
        call_id INTEGER PRIMARY KEY AUTOINCREMENT,
        stage TEXT NOT NULL,
        slot TEXT NOT NULL,
        model_name TEXT NOT NULL,
        question_uid TEXT,
        prompt_version TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL CHECK (status IN ('OK', 'FAILED', 'SCHEMA_RETRY_OK', 'SCHEMA_RETRY_FAILED')),
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        latency_ms INTEGER,
        est_cost REAL,
        raw_response_path TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_llm_calls_stage ON llm_calls(stage, created_at);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_llm_calls_q ON llm_calls(question_uid) WHERE question_uid IS NOT NULL;
    """,
    # 21. FTS5 全文检索虚表
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS questions_fts USING fts5(
        question_uid UNINDEXED,
        grade_subject UNINDEXED,
        question_text,
        visual_facts,
        abstract_skeleton,
        tokenize = 'trigram'
    );
    """,
]

SEED_TRAPS = [
    {
        "code": "TRAP_DOMAIN_BOUNDARY",
        "name": "边界端点与定义域遗漏",
        "description": "忽视端点取值、除数不能为0、几何图形中点线重合或退化、分段区间临界归属误判、实际情境中变量取值范围受限。",
    },
    {
        "code": "TRAP_SIGN_DIRECTION",
        "name": "符号方向与正负歧义",
        "description": "忽略向量/位移/相对运动方向、开方未考虑正负号、移项变号失误、几何翻折旋转手性弄反。",
    },
    {
        "code": "TRAP_EXTRANEOUS_ROOT",
        "name": "增根虚解与几何不可行",
        "description": "方程变形引入不符合原方程的增根、分类讨论后未舍去负时间/越界虚解、代数解无法构成真实几何图形。",
    },
    {
        "code": "TRAP_FRAME_SHIFT",
        "name": "参考系转换与基准漂移",
        "description": "运动问题中错选参考系、百分比/比值基准量前后变化未对齐、面积比例转化中高底基准未对齐。",
    },
    {
        "code": "TRAP_APPROX_CONDITION",
        "name": "近似条件滥用与循环论证",
        "description": "未说明时滥用π近似值、中间步过早四舍五入、进一/去尾法混淆、用待证结论作为前置条件。",
    },
    {
        "code": "TRAP_ARITHMETIC_SLIP",
        "name": "纯四则计算与抄写笔误",
        "description": "进位借位错误、乘法口诀失误、漏抄负号或下标、题目数据抄错。",
    },
    {
        "code": "TRAP_AVERAGE_HARMONIC",
        "name": "调和平均与算术平均混淆",
        "description": "等距往返、上下坡或等额分批采购中，将调和平均速度/单价误按算术平均值 (v1+v2)/2 计算。",
    },
    {
        "code": "TRAP_EXTENDED_BODY_LENGTH",
        "name": "错车超车双车长与参考系混淆",
        "description": "两车相向错车、同向超车或运动行人观察火车问题中，混淆地面看双车长之和与车内窗前看对方单车长、或漏加其中一车车长。",
    },
    {
        "code": "TRAP_BRIDGE_ON_VS_PASS",
        "name": "过桥加车长与在桥减车长混淆",
        "description": "单列火车通过静态桥隧问题中，混淆“完全通过桥隧（路程 = 桥长 + 车长）”、“整列火车完全在桥上（路程 = 桥长 - 车长）”或漏算自身车长。",
    },
    {
        "code": "TRAP_ASYNC_SYNC_CONFUSION",
        "name": "错时异地盲套同步公式",
        "description": "存在提前/延后出发或起点偏移时，未剥离先行段便盲目套用同时同端/双端出发的 1S:3S:5S 等同步倍率结论。",
    },
    {
        "code": "TRAP_DIRECTION_REF_POINT",
        "name": "参照端点与遇追方向混淆",
        "description": "往返相遇或相对运动问题中，混淆“距A地 vs 距B地”参照端点、混淆已走路程与剩余路程、或混淆迎面速度和与同向追及速度差。",
    },
    {
        "code": "TRAP_MIDPOINT_OFFSET_DOUBLE",
        "name": "中点偏移路程差倍半混淆",
        "description": "相向相遇偏离中点或对称点问题中，忽略“两动点路程差 = 2 × 相遇点距中点距离”的倍半关系，误将单倍偏移量当作路程差或将路程差直接当作距中点距离。",
    },
]


def init_db(conn: sqlite3.Connection, config_dir: Path | str | None = None) -> None:
    """初始化数据库架构，创建 20 表 + 1 FTS5 虚表，并按固定顺序执行 3 步 bootstrap。

    Bootstrap 顺序（DESIGN.md §3.2-11）：
    1. 插入 grade_subjects 种子 '小学奥数' (slug='primary_olympiad', subject_family='MATH')
    2. 插入/同步 concepts 12 个预置 TRAP (is_seed=1)
    3. 插入 learners 'default' (display_name='默认', default_grade_subject='小学奥数', is_current=1)
    """
    for stmt in DDL_STATEMENTS:
        conn.execute(stmt)

    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION};")

    # Bootstrap Step 1: grade_subjects 种子
    conn.execute(
        """
        INSERT OR IGNORE INTO grade_subjects(grade_subject, slug, display_name, subject_family)
        VALUES ('小学奥数', 'primary_olympiad', '小学奥数', 'MATH');
        """
    )

    # Bootstrap Step 2: concepts 预置 TRAP（幂等同步新增枚举与最新中文别名/描述）
    traps = SEED_TRAPS
    if config_dir is not None:
        cfg_path = Path(config_dir) / "method_blacklist.yaml"
        if cfg_path.exists():
            try:
                data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
                if data and "seed_traps" in data:
                    traps = data["seed_traps"]
            except Exception:
                pass

    for trap in traps:
        code = trap["code"]
        name = trap.get("name", code)
        desc = trap.get("description", "")
        concept_id = f"TRAP:{code}"
        aliases_json = json.dumps([name], ensure_ascii=False)
        conn.execute(
            """
            INSERT INTO concepts(concept_id, kind, name, aliases_json, description, is_seed)
            VALUES (?, 'TRAP', ?, ?, ?, 1)
            ON CONFLICT(concept_id) DO UPDATE SET
                aliases_json = excluded.aliases_json,
                description = excluded.description,
                is_seed = 1;
            """,
            (concept_id, code, aliases_json, desc),
        )

    # Bootstrap Step 3: learners 'default' 默认角色
    conn.execute(
        """
        INSERT OR IGNORE INTO learners(learner_id, display_name, default_grade_subject, is_current)
        VALUES ('default', '默认', '小学奥数', 1);
        """
    )

    # 建表与初始化完成后，自动安装生效列写守卫（创建 TEMP TRIGGER 拦截 INSERT/REPLACE 绕过）
    from shuati.core.effective import install_eff_write_guard

    install_eff_write_guard(conn)

    conn.commit()
