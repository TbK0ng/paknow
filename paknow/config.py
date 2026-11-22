"""Application settings loaded from environment / .env."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

# Measured on the PA Agent host with a sealed corpus and frozen OKX candidates.
_LOCAL_PA_LIMIT = 8

CloudProvider = Literal["terra", "deepseek"]


@dataclass(frozen=True)
class CloudProfile:
    provider: CloudProvider
    base_url: str
    model: str
    effort: str
    api_key: str = field(repr=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Cloud inference for PA Agent commentary and candidate review.
    pa_cloud_provider: CloudProvider = "terra"
    terra_api_key: str = Field(default="", repr=False)
    terra_base_url: str = "https://api.muzeai.top/v1"
    terra_model: str = "gpt-6.1-terra"
    terra_effort: str = "medium"
    deepseek_api_key: str = Field(default="", repr=False)
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
            explicit = init_settings()
            for profile in ("terra", "deepseek"):
                prefix = profile + "_"
                url, api_key = prefix + "base_url", prefix + "api_key"
                env_url, file_url = env.get(url), local.get(url)
                if file_url and not env_url:
                    # Override a selected .env profile only with an explicit shell URL.
                    env = {key: value for key, value in env.items() if not key.startswith(prefix)}
                elif env_url and str(env_url).rstrip("/") != str(file_url or "").rstrip("/"):
                    # A new endpoint must never inherit the old endpoint's credential.
                    local = {key: value for key, value in local.items() if not key.startswith(prefix)}
                    env.setdefault(api_key, "")
                merged_url = env.get(url, local.get(url, ""))
                if (init_url := explicit.get(url)) and str(init_url).rstrip("/") != str(merged_url).rstrip("/"):
                    env[api_key] = ""
            return {**local, **env}

        return init_settings, provider_aware_settings, file_secret_settings

    @property
    def cloud_profile(self) -> CloudProfile:
        if self.pa_cloud_provider == "terra":
            return CloudProfile("terra", self.terra_base_url, self.terra_model, self.terra_effort, self.terra_api_key)
        return CloudProfile("deepseek", self.deepseek_base_url, self.deepseek_model, self.deepseek_effort, self.deepseek_api_key)

    @property
    def has_cloud(self) -> bool:
        return bool(self.cloud_profile.api_key)

    def ensure_dirs(self) -> None:
        for d in (self.objects_dir, self.models_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()

# Route HuggingFace model downloads to the configured directory before any
# transformers/huggingface_hub import reads the default C-drive cache.
import os

if settings.hf_home:
    os.environ["HF_HOME"] = settings.hf_home
