r"""Repro P2-2: Stage 4 `export -> import` 双通道在零精裁批时卡死 `PENDING_IMPORT`，且在有精裁批但空/残缺结果时静默删边置 `OK`。

覆盖两项确定性缺陷（全部基于临时目录隔离验证）：
1. 零精裁批（`refine_candidates == 0`，`batch_count == 0`，在单侧 `_near_threshold` 与 `--only-uids` 差量织网中高频出现）：
   `run_stage4_weave_async(..., mode="export")` 写出 `stage4_manifest.json(batch_count=0, status="PENDING_IMPORT")`。
   随后执行 `run_stage4_weave_async(..., mode="import")` 成功写入 `RULE_MATRIX` 边并将目标题 `S4` 置 `'OK'`，
   但 `stage4_manifest.json` 仍停留在 `"PENDING_IMPORT"`：
   - `scan_pending_batches()` 永久将其误报为 `NO_RESULTS` 挂起批；
   - 下一轮增量执行 `run_stage4_weave_async(..., mode="export")` 直接被 `PENDING_UNIMPORTED_BATCH` 拦截崩溃！
2. 非空精裁批缺失结果（`refine_candidates > 0`，`batch_count == 1`，传入空/漏批 `results.jsonl`）：
   `run_stage4_weave_async(..., mode="import")` 未核验 `stage4_manifest.json` 中的预期 `batch_ids` 是否全部回传，
   在 `0/1` 批回传时既不报错也不拦截，反而静默丢弃全部待精裁边、清空目标题关系并将 `S4` 置为 `'OK'`。

运行：`.\.venv\Scripts\python.exe tests/repro/repro_p2_stage4_import_manifest_and_incomplete_batches.py`
期望修复后 Exit Code == 0；当前代码实测 Exit Code != 0。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from shuati.config import Settings  # noqa: E402
from shuati.core import effective  # noqa: E402
from shuati.db.connection import PreconditionError, get_connection  # noqa: E402
from shuati.db.schema import init_db  # noqa: E402
from shuati.models.domain import AnswerPart  # noqa: E402
from shuati.pipeline.stage4_weave import run_stage4_weave_async  # noqa: E402
from shuati.services.compiler_tools import scan_pending_batches  # noqa: E402
from shuati.vectors.store import VectorStore  # noqa: E402

GS = "小学奥数"
SLUG = "primary_olympiad"


def _setup_s4_env(tmp: Path, *, k_sim_q1_q2: float) -> tuple[object, Settings, Path, Path, Path]:
    cfg = tmp / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "settings.yaml").write_text("pipeline:\n  use_mock_llm: true\n", encoding="utf-8")

    conn = get_connection(tmp / "s4_repro.db")
    init_db(conn)
    conn.execute("UPDATE grade_subjects SET taxonomy_status='LOCKED', taxonomy_version='1.0' WHERE grade_subject=?;", (GS,))
    for nid in ("n1", "n2", "n3"):
        conn.execute(
            "INSERT INTO knowledge_nodes(node_id, grade_subject, taxonomy_version, node_level, l1_module, l2_entity, l3_operator) VALUES (?, ?, '1.0', 'L3', '平面几何', '等高模型', ?);",
            (nid, GS, f"算子_{nid}"),
        )

    ans = json.dumps(
        [AnswerPart(label="主问", answer_type="NUMERIC", verify_mode="SYMBOLIC", answer="12", sympy_expr="12").model_dump()],
        ensure_ascii=False,
    )
    uids = ["p:c:q1", "p:c:q2", "p:c:q3"]
    for u in uids:
        sid = u.split(":")[-1]
        conn.execute(
            "INSERT INTO raw_questions(question_uid, grade_subject, source_type, source_collection, source_id, raw_question, content_hash) VALUES (?, ?, 'BATCH_JSONL', 'ch01', ?, '求面积', ?);",
            (u, GS, sid, f"h_{sid}"),
        )
        conn.execute(
            "INSERT INTO questions(question_uid, grade_subject, m_question_text, m_format, m_verification_status, m_final_answers_json, m_primary_l1_module, m_c_score, m_s_score, m_t_score, m_l_score) VALUES (?, ?, '求面积', '解答题', 'VERIFIED_CONSENSUS', ?, '平面几何', 2, 2, 1, 1);",
            (u, GS, ans),
        )
        conn.execute(
            "INSERT INTO question_solutions(solution_id, question_uid, branch_id, is_optimal, source_origin, solution_markdown) VALUES (?, ?, 'branch_1', 1, 'PRIMARY_VLM', '解答');",
            (f"{u}::b1", u),
        )
        if u in ("p:c:q1", "p:c:q2"):
            conn.execute(
                "INSERT INTO question_steps(step_id, question_uid, branch_id, step_index, sub_goal, m_l1, m_l2, m_knowledge_node_id) VALUES (?, ?, 'branch_1', 1, '步1', '平面几何', '等高模型', 'n1'), (?, ?, 'branch_1', 2, '步2', '平面几何', '等高模型', 'n2');",
                (f"{u}::s1", u, f"{u}::s2", u),
            )
        else:
            conn.execute(
                "INSERT INTO question_steps(step_id, question_uid, branch_id, step_index, sub_goal, m_l1, m_l2, m_knowledge_node_id) VALUES (?, ?, 'branch_1', 1, '步1', '平面几何', '等高模型', 'n3');",
                (f"{u}::s1", u),
            )
        conn.execute(
            "INSERT INTO compile_state(question_uid, stage, status) VALUES (?,'S1','OK'), (?,'S2','OK'), (?,'S3C','OK'), (?,'S3D','OK'), (?,'S4','STALE');",
            (u, u, u, u, u),
        )
        effective.recompute(conn, u)
    conn.commit()

    vec_dir = tmp / "vecs"
    store = VectorStore(vec_dir)
    surf_vecs = np.eye(3, dtype=np.float32)
    rem = float(max(0.0, 1.0 - k_sim_q1_q2 * k_sim_q1_q2) ** 0.5)
    skel_vecs = np.asarray(
        [
            [1.0, 0.0, 0.0],
            [k_sim_q1_q2, rem, 0.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float32,
    )
    store._save_npz(store.npz_path("surface", SLUG), uids, ["h"] * 3, surf_vecs)
    store._save_npz(store.npz_path("skeleton", SLUG), uids, ["h"] * 3, skel_vecs)
    store._save_meta(
        {
            "embedding_model": "text-embedding-3-large",
            "dimension": 3,
            "meta_version": 1,
            "quantiles": {
                f"{SLUG}::surface": [i / 100.0 for i in range(101)],
                f"{SLUG}::skeleton": [i / 100.0 for i in range(101)],
            },
            "scopes": {SLUG: {"n0": 3, "l1_dist": {"平面几何": 1.0}, "updated_at": "2026-11-25T00:00:00Z"}},
        }
    )

    settings = Settings()
    settings.pipeline.use_mock_llm = True
    settings.topology.thresholds.isomorphic.skeleton_pct = 0.80
    uni_root = tmp / "unified"
    return conn, settings, cfg, vec_dir, uni_root


async def _check_case_1_zero_refine_manifest_stuck(base_tmp: Path) -> None:
    conn, settings, cfg, vec_dir, uni_root = _setup_s4_env(base_tmp / "c1_zero_refine", k_sim_q1_q2=0.86)
    tasks_dir = uni_root / "stage3_4" / "stage4"
    orig_sync = VectorStore.sync_vectors

    async def _noop_sync(self, ns, items, client, **kw):
        return {"total": len(items), "reused": len(items), "embedded": 0, "removed": 0, "dimension": 3}

    VectorStore.sync_vectors = _noop_sync  # type: ignore[method-assign]
    try:
        exp1 = await run_stage4_weave_async(
            conn, settings, grade_subject=GS, mode="export", tasks_dir=tasks_dir, vectors_dir=vec_dir,
            lock_path=base_tmp / "c1.lock", config_dir=cfg, use_mock=True, only_uids=["p:c:q1"],
        )
        assert exp1["refine_candidates"] == 0 and exp1["auto_candidates"] == 1

        res_file = tasks_dir / "results.jsonl"
        res_file.write_text("", encoding="utf-8")
        await run_stage4_weave_async(
            conn, settings, grade_subject=GS, mode="import", tasks_dir=tasks_dir, results_path=res_file,
            vectors_dir=vec_dir, lock_path=base_tmp / "c1.lock", config_dir=cfg, use_mock=True,
        )

        manifest = json.loads((tasks_dir / "stage4_manifest.json").read_text(encoding="utf-8"))
        assert manifest.get("status") == "IMPORTED", (
            f"Case 1 缺陷：0 精裁批完成 import 后 stage4_manifest.json 状态仍为 {manifest.get('status')!r} 而非 'IMPORTED'"
        )
        pending = scan_pending_batches(uni_root)
        assert pending == [], f"Case 1 缺陷：0 精裁批完成 import 后 scan_pending_batches 仍残留挂起项: {pending}"

        # 紧接着对下一题执行增量 export 不应被 PENDING_UNIMPORTED_BATCH 拦截
        await run_stage4_weave_async(
            conn, settings, grade_subject=GS, mode="export", tasks_dir=tasks_dir, vectors_dir=vec_dir,
            lock_path=base_tmp / "c1.lock", config_dir=cfg, use_mock=True, only_uids=["p:c:q3"],
        )
    finally:
        VectorStore.sync_vectors = orig_sync  # type: ignore[method-assign]
        conn.close()  # type: ignore[attr-defined]


async def _check_case_2_missing_batches_silent_import(base_tmp: Path) -> None:
    # k_sim=0.72 落入精裁候选池，产生 1 个精裁批 (batch_count == 1)
    conn, settings, cfg, vec_dir, uni_root = _setup_s4_env(base_tmp / "c2_missing_batch", k_sim_q1_q2=0.72)
    tasks_dir = uni_root / "stage3_4" / "stage4"
    orig_sync = VectorStore.sync_vectors

    async def _noop_sync(self, ns, items, client, **kw):
        return {"total": len(items), "reused": len(items), "embedded": 0, "removed": 0, "dimension": 3}

    VectorStore.sync_vectors = _noop_sync  # type: ignore[method-assign]
    try:
        exp = await run_stage4_weave_async(
            conn, settings, grade_subject=GS, mode="export", tasks_dir=tasks_dir, vectors_dir=vec_dir,
            lock_path=base_tmp / "c2.lock", config_dir=cfg, use_mock=True, only_uids=["p:c:q1"],
        )
        assert exp["refine_candidates"] == 1
        assert exp["manifest"]["batch_count"] == 1

        # 传入完全空白的 results.jsonl（0/1 批回传），import 必须拒绝并抛 PreconditionError，严禁静默把 S4 置为 OK
        empty_res = tasks_dir / "results.jsonl"
        empty_res.write_text("", encoding="utf-8")
        try:
            await run_stage4_weave_async(
                conn, settings, grade_subject=GS, mode="import", tasks_dir=tasks_dir, results_path=empty_res,
                vectors_dir=vec_dir, lock_path=base_tmp / "c2.lock", config_dir=cfg, use_mock=True,
            )
        except PreconditionError:
            s4_st = conn.execute("SELECT status FROM compile_state WHERE question_uid='p:c:q1' AND stage='S4';").fetchone()[0]  # type: ignore[attr-defined]
            assert s4_st == "STALE"
            return

        s4_st = conn.execute("SELECT status FROM compile_state WHERE question_uid='p:c:q1' AND stage='S4';").fetchone()[0]  # type: ignore[attr-defined]
        raise AssertionError(
            f"Case 2 缺陷：预期 1 个精裁批但回传 0 批时，Stage 4 import 未抛 PreconditionError，反而静默将 q1 S4 置为 {s4_st!r}！"
        )
    finally:
        VectorStore.sync_vectors = orig_sync  # type: ignore[method-assign]
        conn.close()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_repro_p2_stage4_import_manifest_and_incomplete_batches(tmp_path: Path) -> None:
    await _check_case_1_zero_refine_manifest_stuck(tmp_path)
    await _check_case_2_missing_batches_silent_import(tmp_path)


async def _amain() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    tmp = Path(tempfile.mkdtemp(prefix="repro_p2_s4_"))
    try:
        errors: list[str] = []
        for label, fn in (
            ("Case1_ZeroRefine_ManifestStuck", _check_case_1_zero_refine_manifest_stuck),
            ("Case2_MissingBatch_SilentImport", _check_case_2_missing_batches_silent_import),
        ):
            try:
                await fn(tmp)
                print(f"[PASS] {label}")
            except Exception as exc:
                msg = f"[FAIL] {label}: {type(exc).__name__}: {exc}"
                print(msg)
                errors.append(msg)
        return 1 if errors else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_amain()))
