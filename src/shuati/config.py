"""配置加载与模型网关槽位管理。"""

from __future__ import annotations

import difflib
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator


def _resolve_env_vars(text: str) -> str:
    """展开字符串中的 ${VAR} 与 ${VAR:-default}。"""
    pattern = re.compile(r"\$\{([A-Za-z0-9_]+)(?::-([^}]*))?\}")

    def replace_match(match: re.Match[str]) -> str:
        var_name = match.group(1)
        default_val = match.group(2) if match.group(2) is not None else ""
        return os.environ.get(var_name, default_val)

    return pattern.sub(replace_match, text)


def _load_env_file(env_path: Path) -> None:
    """若存在 .env 文件则加载至 os.environ（不覆盖已有环境变量）。"""
    if not env_path.exists():
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            else:
                v = re.split(r"[ \t]+#", v, maxsplit=1)[0].strip()
            if k not in os.environ:
                os.environ[k] = v


class LLMSlotConfig(BaseModel):
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = ""
    temperature: float = 0.1
    timeout_seconds: float = 120.0
    max_concurrency: int = 4
    rpm_limit: int = 60
    max_retries: int = 3


class EmbeddingConfig(BaseModel):
    provider: str = "openai_compatible"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "text-embedding-3-large"
    batch_size: int = 32
    timeout_seconds: float = 60.0
    max_concurrency: int = 8
    rpm_limit: int = 120
    max_retries: int = 3


class LLMConfig(BaseModel):
    vlm_primary: LLMSlotConfig = Field(default_factory=LLMSlotConfig)
    vlm_secondary: LLMSlotConfig = Field(default_factory=LLMSlotConfig)
    text_llm: LLMSlotConfig = Field(default_factory=LLMSlotConfig)
    judge_llm: LLMSlotConfig = Field(default_factory=LLMSlotConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)


class PipelineConfig(BaseModel):
    dual_vlm_policy: str = "always"
    sympy_timeout_seconds: float = 5.0
    enable_stage4_llm_refine: bool = True
    use_mock_llm: bool = False
    max_stage_attempts: int = 3
    sympy_memory_limit_mb: int = 512
    consensus_audit_ratio: float = 0.05
    max_external_image_px: int = 2048
    allow_pi_314_subjects: list[str] = Field(default_factory=lambda: ["小学奥数"])


class LearnerEloConfig(BaseModel):
    initial: float = 1300.0
    k_provisional: float = 48.0
    k_stable: float = 24.0
    provisional_attempts: int = 10
    hint_score: float = 0.5
    repeat_within_days: int = 7
    secondary_l1_weight: float = 0.5


class ComposerConfig(BaseModel):
    exclude_solved_days: int = 60
    exclude_clone_days: int = 14
    exclude_recent_quiz_batches: int = 3


class CloneThresholdsConfig(BaseModel):
    knn_k: int = 3
    surface_pct: float = 0.995
    surface_sim_min: float = 0.85
    skeleton_pct: float = 0.995
    skeleton_sim_min: float = 0.88
    max_elo_diff: int = 120
    degree_cap: int = 5


class IsomorphicThresholdsConfig(BaseModel):
    knn_k: int = 10
    skeleton_pct: float = 0.98
    skeleton_sim_min: float = 0.76
    surface_pct_max: float = 0.60
    k_jaccard_min: float = 0.45
    max_elo_diff: int = 180
    boundary_band: float = 0.10
    degree_cap: int = 8


class ScaffoldThresholdsConfig(BaseModel):
    min_k_size: int = 2
    containment_min: float = 0.80
    reverse_containment_max: float = 0.60
    min_elo_diff: int = 200
    skeleton_pct_min: float = 0.90
    boundary_band: float = 0.10
    in_degree_cap: int = 5
    out_degree_cap: int = 5


class ContrastiveThresholdsConfig(BaseModel):
    knn_k: int = 10
    surface_pct_min: float = 0.98
    k_jaccard_max: float = 0.35
    degree_cap: int = 5


class SharedTrapThresholdsConfig(BaseModel):
    min_shared: int = 1
    idf_overlap_min: float = 0.65
    skeleton_pct_max: float = 0.60
    cross_l1_bonus: float = 1.3
    degree_cap: int = 5


class TopologyThresholdsConfig(BaseModel):
    clone: CloneThresholdsConfig = Field(default_factory=CloneThresholdsConfig)
    isomorphic: IsomorphicThresholdsConfig = Field(default_factory=IsomorphicThresholdsConfig)
    scaffold: ScaffoldThresholdsConfig = Field(default_factory=ScaffoldThresholdsConfig)
    contrastive: ContrastiveThresholdsConfig = Field(default_factory=ContrastiveThresholdsConfig)
    shared_trap: SharedTrapThresholdsConfig = Field(default_factory=SharedTrapThresholdsConfig)


class TopologyConfig(BaseModel):
    rule_version: str = "2.4-init"
    refine_pool_multiplier: int = Field(default=3, ge=1)
    expected_degree: dict[str, list[int]] = Field(
        default_factory=lambda: {
            "CLONE": [0, 1],
            "ISOMORPHIC": [1, 5],
            "SCAFFOLD": [1, 6],
            "CONTRASTIVE": [0, 2],
            "SHARED_TRAP": [0, 5],
        }
    )
    thresholds: TopologyThresholdsConfig = Field(default_factory=TopologyThresholdsConfig)


class HumanMirrorConfig(BaseModel):
    dir: str = "human/"
    include_practice_log: bool = True
    backups_keep: int = 10


class IngestConfig(BaseModel):
    roots: list[str] = Field(default_factory=lambda: ["题库"])
    dedup_near_default: bool = True


KNOWN_SUBAGENT_MODELS: frozenset[str] = frozenset(
    {
        "gemini-4-neon-medium",
        "gemini-4-neon-high",
        "gemini-4-neon-low",
        "gemini-4-neon-xhigh",
        "gemini-4-carbon-high",
        "gemini-4-carbon-medium",
        "gemini-4-hydrogen-high",
        "gemini-4-hydrogen-medium",
        "gemini-4-argon-high",
        "gemini-4-argon-medium",
        "gemini-3.8-flash-high",
        "gemini-3.8-flash-medium",
        "claude-sonnet-5-5-high",
        "claude-sonnet-5-5-medium",
        "claude-opus-5-5-high",
        "claude-opus-5-5-medium",
        "gpt-oss-120b-medium",
    }
)

_SUBAGENT_MODEL_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")


def validate_subagent_model_id(model_id: str) -> str:
    """校验 Subagent 模型 ID：拦截常见拼写错误并给出标准候选建议。"""
    cleaned = (model_id or "").strip()
    if not cleaned:
        raise ValueError("Subagent 模型 ID 不能为空")
    if cleaned in KNOWN_SUBAGENT_MODELS:
        return cleaned
    if not _SUBAGENT_MODEL_SLUG_RE.match(cleaned):
        raise ValueError(
            f"非法 Subagent 模型 ID 格式: {cleaned!r}（仅允许字母/数字/点/下划线/短横线）"
        )
    close_matches = difflib.get_close_matches(
        cleaned.lower(), sorted(KNOWN_SUBAGENT_MODELS), n=3, cutoff=0.75
    )
    if close_matches:
        raise ValueError(
            f"无效或疑似拼写错误的 Subagent 模型 ID: {cleaned!r}；"
            f"您是否想使用: {', '.join(close_matches)}？"
        )
    return cleaned


class SubagentConfig(BaseModel):
    default_model: str = "gemini-4-neon-medium"
    max_parallel_workers: int = Field(default=4, ge=1)
    stage_models: dict[str, str] = Field(default_factory=dict)

    @field_validator("default_model")
    @classmethod
    def _validate_default_model(cls, value: str) -> str:
        return validate_subagent_model_id(value)

    @field_validator("stage_models")
    @classmethod
    def _validate_stage_models(cls, value: dict[str, str]) -> dict[str, str]:
        return {
            str(k).strip(): validate_subagent_model_id(v)
            for k, v in value.items()
            if str(k).strip()
        }


class Settings(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    timezone: str = "Asia/Shanghai"
    learner_elo: LearnerEloConfig = Field(default_factory=LearnerEloConfig)
    composer: ComposerConfig = Field(default_factory=ComposerConfig)
    topology: TopologyConfig = Field(default_factory=TopologyConfig)
    human_mirror: HumanMirrorConfig = Field(default_factory=HumanMirrorConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    subagent: SubagentConfig = Field(default_factory=SubagentConfig)


def resolve_subagent_model(
    settings: Settings,
    stage: str,
    cli_override: str | None = None,
) -> str:
    """按 CLI 覆写 > stage_models[stage] > default_model 优先级解析并校验 Subagent 模型 ID。"""
    if cli_override is not None and cli_override.strip():
        return validate_subagent_model_id(cli_override)
    stage_key = (stage or "").strip()
    stage_models = settings.subagent.stage_models or {}
    if stage_key in stage_models and stage_models[stage_key].strip():
        return validate_subagent_model_id(stage_models[stage_key])
    lowered = stage_key.lower()
    for k, v in stage_models.items():
        if k.strip().lower() == lowered and v.strip():
            return validate_subagent_model_id(v)
    return validate_subagent_model_id(settings.subagent.default_model)


def load_settings(config_path: Path | str | None = None, env_path: Path | str | None = None) -> Settings:
    """加载配置并展开环境变量，同时处理 judge_llm 回退逻辑。"""
    root_dir = Path.cwd()
    if env_path is None:
        env_path = root_dir / ".env"
    _load_env_file(Path(env_path))

    if config_path is None:
        config_path = root_dir / "config" / "settings.yaml"
    else:
        config_path = Path(config_path)
        if config_path.is_dir():
            config_path = config_path / "settings.yaml"

    raw_dict: dict[str, Any] = {}
    if config_path.exists():
        raw_text = config_path.read_text(encoding="utf-8")
        interpolated = _resolve_env_vars(raw_text)
        raw_dict = yaml.safe_load(interpolated) or {}

    settings = Settings.model_validate(raw_dict)

    # 规范与红线：judge_llm 未配置时自动回退复用 text_llm 且 temperature=0.0
    if not settings.llm.judge_llm.model:
        settings.llm.judge_llm = settings.llm.text_llm.model_copy()
        settings.llm.judge_llm.temperature = 0.0

    return settings


_GLOBAL_SETTINGS: Settings | None = None


def get_settings() -> Settings:
    global _GLOBAL_SETTINGS
    if _GLOBAL_SETTINGS is None:
        _GLOBAL_SETTINGS = load_settings()
    return _GLOBAL_SETTINGS
