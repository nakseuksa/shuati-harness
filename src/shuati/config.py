"""配置加载与模型网关槽位管理。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


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
            v = v.strip().strip("'\"")
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


class TopologyConfig(BaseModel):
    rule_version: str = "2.4-init"
    expected_degree: dict[str, list[int]] = Field(
        default_factory=lambda: {
            "CLONE": [0, 1],
            "ISOMORPHIC": [1, 5],
            "SCAFFOLD": [1, 6],
            "CONTRASTIVE": [0, 2],
            "SHARED_TRAP": [0, 5],
        }
    )


class HumanMirrorConfig(BaseModel):
    dir: str = "human/"
    include_practice_log: bool = True
    backups_keep: int = 10


class Settings(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    timezone: str = "Asia/Shanghai"
    learner_elo: LearnerEloConfig = Field(default_factory=LearnerEloConfig)
    composer: ComposerConfig = Field(default_factory=ComposerConfig)
    topology: TopologyConfig = Field(default_factory=TopologyConfig)
    human_mirror: HumanMirrorConfig = Field(default_factory=HumanMirrorConfig)


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
