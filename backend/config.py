"""集中配置：算法超参数 / 模型网关 / 沙盒配额 / 存储路径。

所有确定性常量统一在此管理（设计文档 Target File #3），禁止在业务代码中硬编码魔数。
支持环境变量与 .env 覆盖（pydantic-settings）。
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---- 模型网关（OpenAI 兼容接口） ----
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://api.openai.com/v1"
    VLM_MODEL_NAME: str = "gpt-4o"
    SOLVER_LLM_MODEL_NAME: str = "gpt-4o-mini"
    LLM_TEMPERATURE: float = 0.1
    LLM_MAX_RETRIES: int = 2
    USE_MOCK_LLM: bool = False

    # ---- 存储路径 ----
    DATA_DIR: Path = Path("./data")
    SQLITE_PATH: Path = Path("./data/harness.db")
    CAS_ASSETS_DIR: Path = Path("./data/assets")
    CAS_MAX_IMAGE_SIDE: int = 1536

    # ---- 受限沙盒配额 ----
    SYMPY_TIMEOUT_SECONDS: float = 5.0
    SYMPY_MAX_MEMORY_MB: int = 256
    SYMPY_MAX_RETRIES: int = 2

    # ---- 知识网阈值 ----
    L3_LOCAL_RECALL_TRIGGER_COUNT: int = 25
    L3_TOP_K_CANDIDATES: int = 15
    MAX_ALIASES_PER_NODE: int = 8
    COMPACTION_BATCH_THRESHOLD: int = 15

    # ---- 盲测调度 ----
    DAILY_BLIND_DRILL_CAP: int = 3
    BLIND_WINDOW_CONCEPT_DAYS: tuple[int, int] = (3, 7)
    BLIND_WINDOW_TRAP_DAYS: tuple[int, int] = (2, 4)
    DECAY_EXPOSURE_THRESHOLD: int = 3
    ESCROW_DAILY_DECAY: float = 0.15

    # ---- 双轨 Elo 与难度合成 ----
    MODULE_ELO_INITIAL: float = 1200.0
    K_INIT: float = 24.0
    K_FLOOR: float = 16.0
    K_COV: float = 32.0
    K_COMBAT: float = 24.0
    SUBGOAL_ELO_MARGIN: int = 150
    BASE_ELO_MIN: int = 800
    BASE_ELO_MAX: int = 2800
    SUB_ELO_MAX: int = 2650
    WATERMARK_FULL_MARGIN: int = 200
    WATERMARK_ZERO_MARGIN: int = 500
    WATERMARK_GAUSS_SIGMA: float = 150.0


settings = Settings()
