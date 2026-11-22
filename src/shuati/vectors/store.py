"""本地向量仓管理（.npz 矩阵 + 跨进程锁 + meta.json 冻结分位 LUT）。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Literal

import numpy as np

from shuati.db.connection import CompileLock, PreconditionError
from shuati.llm.embeddings import EmbeddingClient

VectorNamespace = Literal[
    "surface",
    "skeleton",
    "l3_nodes",
    "concepts_method",
    "concepts_motif",
]


def compute_text_hash(text: str) -> str:
    """计算文本规范化后的 SHA-256 指纹。"""
    normalized = " ".join((text or "").split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _atomic_write_bytes(target: Path, write_fn: Any) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as f:
            write_fn(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


class VectorStore:
    """统一管理 5 类 .npz 向量矩阵与 meta.json 冻结分位表。"""

    def __init__(self, vectors_dir: Path | str = Path("data/vectors")) -> None:
        self.vectors_dir = Path(vectors_dir)
        self.lock_path = self.vectors_dir / ".lock"
        self.meta_path = self.vectors_dir / "meta.json"

    def _lock(self) -> CompileLock:
        return CompileLock(self.lock_path)

    def npz_path(self, namespace: VectorNamespace, scope_slug: str | None = None) -> Path:
        if namespace in ("concepts_method", "concepts_motif"):
            return self.vectors_dir / f"{namespace}.npz"
        if not scope_slug:
            raise ValueError(f"namespace={namespace!r} 必须提供 scope_slug")
        return self.vectors_dir / f"{namespace}_{scope_slug}.npz"

    def load_meta(self) -> dict[str, Any]:
        if not self.meta_path.exists():
            return {
                "embedding_model": "",
                "dimension": 0,
                "meta_version": 0,
                "updated_at": "",
                "quantiles": {},
            }
        data = json.loads(self.meta_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {
                "embedding_model": "",
                "dimension": 0,
                "meta_version": 0,
                "updated_at": "",
                "quantiles": {},
            }
        data.setdefault("embedding_model", "")
        data.setdefault("dimension", 0)
        data.setdefault("meta_version", 0)
        data.setdefault("updated_at", "")
        data.setdefault("quantiles", {})
        return data

    def _save_meta(self, meta: dict[str, Any]) -> None:
        meta["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        payload = json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8")
        _atomic_write_bytes(self.meta_path, lambda f: f.write(payload))

    def validate_model_match(self, model_name: str, *, allow_empty: bool = True) -> None:
        """检查当前配置模型是否与 meta.json 一致；切换模型未全量重建时阻断。"""
        meta = self.load_meta()
        recorded = str(meta.get("embedding_model") or "")
        if not recorded:
            if allow_empty:
                return
            raise PreconditionError(
                "PRECONDITION_FAILED: data/vectors/meta.json 尚未初始化，请先执行 `shuati reindex --vectors --quantiles`。"
            )
        if recorded != model_name:
            raise PreconditionError(
                f"PRECONDITION_FAILED: Embedding 模型已切换 ({recorded!r} -> {model_name!r})，"
                "请先执行 `shuati reindex --vectors --quantiles` 全量重建向量仓与分位表。"
            )

    def load_vectors(
        self,
        namespace: VectorNamespace,
        scope_slug: str | None = None,
    ) -> tuple[list[str], np.ndarray]:
        """读取 .npz 中的 (ids, vectors)。文件不存在时返回空列表与空矩阵。"""
        path = self.npz_path(namespace, scope_slug)
        if not path.exists():
            return [], np.zeros((0, 0), dtype=np.float32)
        with np.load(path, allow_pickle=False) as data:
            ids = [str(x) for x in data["ids"].tolist()]
            vecs = np.asarray(data["vectors"], dtype=np.float32)
            return ids, vecs

    def _load_raw_npz(self, path: Path) -> tuple[list[str], list[str], np.ndarray]:
        if not path.exists():
            return [], [], np.zeros((0, 0), dtype=np.float32)
        with np.load(path, allow_pickle=False) as data:
            ids = [str(x) for x in data["ids"].tolist()]
            hashes = [str(x) for x in data["text_hashes"].tolist()]
            vecs = np.asarray(data["vectors"], dtype=np.float32)
            return ids, hashes, vecs

    def _save_npz(
        self,
        path: Path,
        ids: list[str],
        hashes: list[str],
        vecs: np.ndarray,
    ) -> None:
        id_arr = np.asarray(ids, dtype=str)
        hash_arr = np.asarray(hashes, dtype=str)
        vec_arr = np.asarray(vecs, dtype=np.float32)
        _atomic_write_bytes(
            path,
            lambda f: np.savez(f, ids=id_arr, text_hashes=hash_arr, vectors=vec_arr),
        )

    async def sync_vectors(
        self,
        namespace: VectorNamespace,
        items: list[tuple[str, str]],
        client: EmbeddingClient,
        *,
        scope_slug: str | None = None,
        force_rebuild: bool = False,
        stage: str = "EMBED",
    ) -> dict[str, int]:
        """按文本 SHA-256 差量同步向量矩阵。"""
        deduped: dict[str, str] = {}
        for item_id, text in items:
            deduped[str(item_id)] = text or ""
        ordered_ids = list(deduped.keys())
        target_hashes = [compute_text_hash(deduped[i]) for i in ordered_ids]

        path = self.npz_path(namespace, scope_slug)
        with self._lock():
            meta = self.load_meta()
            recorded_model = str(meta.get("embedding_model") or "")
            if recorded_model and recorded_model != client.model_name and not force_rebuild:
                raise PreconditionError(
                    f"PRECONDITION_FAILED: Embedding 模型已切换 ({recorded_model!r} -> {client.model_name!r})，"
                    "请先执行 `shuati reindex --vectors --quantiles` 全量重建向量仓与分位表。"
                )
            old_ids, old_hashes, old_vecs = self._load_raw_npz(path)

        old_dim = old_vecs.shape[1] if old_vecs.ndim == 2 and old_vecs.shape[0] > 0 else 0
        old_map: dict[str, tuple[str, np.ndarray]] = {}
        if not force_rebuild and len(old_ids) == len(old_hashes) == len(old_vecs):
            for idx, oid in enumerate(old_ids):
                old_map[oid] = (old_hashes[idx], old_vecs[idx])

        miss_indices: list[int] = []
        miss_texts: list[str] = []
        reused = 0
        for idx, item_id in enumerate(ordered_ids):
            hit = old_map.get(item_id)
            if hit is not None and hit[0] == target_hashes[idx] and (old_dim == 0 or hit[1].shape[0] == old_dim):
                reused += 1
            else:
                miss_indices.append(idx)
                miss_texts.append(deduped[item_id])

        new_vecs = (
            await client.embed_texts(miss_texts, stage=stage)
            if miss_texts
            else np.zeros((0, old_dim), dtype=np.float32)
        )
        dim = int(new_vecs.shape[1]) if new_vecs.shape[0] > 0 else old_dim
        if dim == 0 and ordered_ids:
            # 理论上不会发生，兜底维度
            dim = client.MOCK_DIM

        final_vecs = np.zeros((len(ordered_ids), dim), dtype=np.float32)
        miss_cursor = 0
        miss_set = set(miss_indices)
        for idx, item_id in enumerate(ordered_ids):
            if idx in miss_set:
                final_vecs[idx] = new_vecs[miss_cursor]
                miss_cursor += 1
            else:
                final_vecs[idx] = old_map[item_id][1]

        removed = max(0, len(set(old_ids) - set(ordered_ids)))

        with self._lock():
            meta = self.load_meta()
            recorded_model = str(meta.get("embedding_model") or "")
            if recorded_model and recorded_model != client.model_name and not force_rebuild:
                raise PreconditionError(
                    f"PRECONDITION_FAILED: Embedding 模型已切换 ({recorded_model!r} -> {client.model_name!r})，"
                    "请先执行 `shuati reindex --vectors --quantiles`。"
                )
            self._save_npz(path, ordered_ids, target_hashes, final_vecs)
            meta["embedding_model"] = client.model_name
            if dim > 0:
                meta["dimension"] = dim
            if force_rebuild and recorded_model and recorded_model != client.model_name:
                meta["quantiles"] = {}
            self._save_meta(meta)

        return {
            "total": len(ordered_ids),
            "reused": reused,
            "embedded": len(miss_indices),
            "removed": removed,
            "dimension": dim,
        }

    def sync_vectors_sync(
        self,
        namespace: VectorNamespace,
        items: list[tuple[str, str]],
        client: EmbeddingClient,
        *,
        scope_slug: str | None = None,
        force_rebuild: bool = False,
        stage: str = "EMBED",
    ) -> dict[str, int]:
        """同步包装接口。"""
        return asyncio.run(
            self.sync_vectors(
                namespace,
                items,
                client,
                scope_slug=scope_slug,
                force_rebuild=force_rebuild,
                stage=stage,
            )
        )

    def clear_model_binding_for_rebuild(self, scope_slugs: list[str] | None = None) -> None:
        """强制清空已有向量文件，用于模型切换全量重建。"""
        with self._lock():
            for p in self.vectors_dir.glob("*.npz"):
                p.unlink(missing_ok=True)
            meta = self.load_meta()
            meta["embedding_model"] = ""
            meta["dimension"] = 0
            meta["quantiles"] = {}
            self._save_meta(meta)

    def rebuild_quantiles(
        self,
        scope_slug: str,
        channels: tuple[Literal["surface", "skeleton"], ...] = ("surface", "skeleton"),
        *,
        sample_pairs: int = 20000,
        seed: int = 42,
    ) -> dict[str, Any]:
        """按学段学科与通道抽样计算 101 点经验分位 LUT 并递增 meta_version。"""
        with self._lock():
            meta = self.load_meta()
            quantiles = dict(meta.get("quantiles", {}))
            summary: dict[str, Any] = {}

            for ch in channels:
                ids, vecs = self.load_vectors(ch, scope_slug=scope_slug)
                n = len(ids)
                key = f"{scope_slug}::{ch}"
                if n < 2 or vecs.shape[0] < 2:
                    lut = [round(i / 100.0, 6) for i in range(101)]
                    quantiles[key] = lut
                    summary[key] = {"n": n, "sampled_pairs": 0, "p50": lut[50], "p99": lut[99]}
                    continue

                total_pairs = n * (n - 1) // 2
                if total_pairs <= sample_pairs:
                    sim_mat = vecs @ vecs.T
                    iu, ju = np.triu_indices(n, k=1)
                    sims = sim_mat[iu, ju]
                else:
                    rng = np.random.default_rng(seed)
                    # 过采样后去重，稳定取样至多 sample_pairs 对无序异下标对
                    batch = int(sample_pairs * 1.4) + 64
                    i_idx = rng.integers(0, n, size=batch)
                    j_idx = rng.integers(0, n, size=batch)
                    mask = i_idx != j_idx
                    i_idx = i_idx[mask]
                    j_idx = j_idx[mask]
                    lo = np.minimum(i_idx, j_idx)
                    hi = np.maximum(i_idx, j_idx)
                    pair_keys = lo.astype(np.int64) * np.int64(n) + hi.astype(np.int64)
                    _, uniq_pos = np.unique(pair_keys, return_index=True)
                    uniq_pos = np.sort(uniq_pos)[:sample_pairs]
                    lo = lo[uniq_pos]
                    hi = hi[uniq_pos]
                    sims = np.sum(vecs[lo] * vecs[hi], axis=1)

                pct_grid = np.linspace(0.0, 1.0, 101)
                lut_arr = np.quantile(sims, pct_grid)
                lut = [float(x) for x in lut_arr.tolist()]
                quantiles[key] = lut
                summary[key] = {
                    "n": n,
                    "sampled_pairs": int(sims.shape[0]),
                    "p50": lut[50],
                    "p95": lut[95],
                    "p99": lut[99],
                }

            meta["quantiles"] = quantiles
            meta["meta_version"] = int(meta.get("meta_version", 0)) + 1
            self._save_meta(meta)
            return {
                "meta_version": meta["meta_version"],
                "channels": summary,
            }

    def has_quantiles(self, scope_slug: str, channel: str) -> bool:
        meta = self.load_meta()
        key = f"{scope_slug}::{channel}"
        lut = meta.get("quantiles", {}).get(key)
        return isinstance(lut, list) and len(lut) >= 2

    def lookup_percentile(self, scope_slug: str, channel: str, sim: float) -> float:
        """查冻结分位 LUT 线性插值计算相似度经验分位数 [0.0, 1.0]。"""
        meta = self.load_meta()
        key = f"{scope_slug}::{channel}"
        lut = meta.get("quantiles", {}).get(key)
        if not isinstance(lut, list) or len(lut) < 2:
            raise PreconditionError(
                f"PRECONDITION_FAILED: 缺失冻结分位表 {key!r}，请先执行 `shuati reindex --quantiles`。"
            )
        xp = np.asarray(lut, dtype=np.float64)
        fp = np.linspace(0.0, 1.0, len(lut), dtype=np.float64)
        pct = float(np.interp(float(sim), xp, fp))
        return max(0.0, min(1.0, pct))
