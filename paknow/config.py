"""Application settings loaded from environment / .env."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

# Measured on the PA Agent host with a sealed corpus and frozen OKX candidates.
_LOCAL_PA_LIMIT = 8


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Cloud inference for PA Agent commentary and candidate review.
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://www.micuapi.ai/v1"
    deepseek_model: str = "deepseek-v4.1-flash"
    deepseek_effort: str = "high"
    pa_review_max_tokens: int = Field(default=24_576, ge=2048, le=65_536)

    # Local infrastructure.
    # SQLite is the zero-setup default; switch to Postgres for production:
    #   postgresql+psycopg://paknow:paknow@localhost:5432/paknow
    paknow_db_url: str = "sqlite:///./data/paknow.db"
    # Empty = embedded Qdrant (local mode); server URL e.g. http://localhost:6333
    qdrant_url: str = ""

    # Paths. Original source files may live outside the repo; set
    # PAKNOW_CORPUS_DIR to point at them. Derived objects and model weights
    # live under data/.
    project_root: Path = Path(__file__).resolve().parent.parent
    corpus_dir: Path = Field(
        default=Path(__file__).resolve().parent.parent,
        validation_alias="PAKNOW_CORPUS_DIR",
    )
    objects_dir: Path = project_root / "data" / "objects"
    models_dir: Path = project_root / "data" / "models"
    hf_home: str = r"E:\use_model"

    # Indexing budget knobs (initial values; tuned later).
    embed_model: str = "BAAI/bge-m3"
    embed_batch_size: int = 32
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    pa_local_parallelism: int = Field(default=_LOCAL_PA_LIMIT, ge=1, le=_LOCAL_PA_LIMIT)

    @classmethod
    def settings_customise_sources(
        cls, settings_cls: type[BaseSettings], init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource, dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple:
        def provider_aware_settings() -> dict[str, Any]:
            env, local = env_settings(), dotenv_settings()
            env_url, file_url = env.get("deepseek_base_url"), local.get("deepseek_base_url")
            if file_url and not env_url:
                # A selected local provider must not receive a shell's key for
                # a different provider. Override it with an explicit env URL.
                env = {key: value for key, value in env.items() if not key.startswith("deepseek_")}
            elif env_url and str(env_url).rstrip("/") != str(file_url or "").rstrip("/"):
                # An explicit new endpoint never inherits the local provider key.
                local = {key: value for key, value in local.items() if not key.startswith("deepseek_")}
                env.setdefault("deepseek_api_key", "")
            merged = {**local, **env}
            explicit = init_settings()
            init_url = explicit.get("deepseek_base_url")
            if init_url and str(init_url).rstrip("/") != str(merged.get("deepseek_base_url", "")).rstrip("/"):
                merged["deepseek_api_key"] = ""
            return merged

        return init_settings, provider_aware_settings, file_secret_settings

    @property
    def has_cloud(self) -> bool:
        return bool(self.deepseek_api_key)

    def ensure_dirs(self) -> None:
        for d in (self.objects_dir, self.models_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()

# Route HuggingFace model downloads to the configured directory before any
# transformers/huggingface_hub import reads the default C-drive cache.
import os

if settings.hf_home:
    os.environ["HF_HOME"] = settings.hf_home
