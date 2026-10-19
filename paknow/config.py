"""Application settings loaded from environment / .env."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Cloud inference (answer / routing / chart observation).
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-flash"
    deepseek_effort: str = "high"
    deepseek_vision: bool = False

    # Local infrastructure.
    # SQLite is the zero-setup default; switch to Postgres for production:
    #   postgresql+psycopg://paknow:paknow@localhost:5432/paknow
    paknow_db_url: str = "sqlite:///./data/paknow.db"
    # Empty = embedded Qdrant (local mode); server URL e.g. http://localhost:6333
    qdrant_url: str = ""

    # Paths. Original source files stay in place (project root); derived
    # objects and model weights live under data/.
    project_root: Path = Path(__file__).resolve().parent.parent
    objects_dir: Path = project_root / "data" / "objects"
    models_dir: Path = project_root / "data" / "models"
    hf_home: str = r"E:\use_model"

    # Indexing budget knobs (initial values; tuned later).
    embed_model: str = "BAAI/bge-m3"
    embed_batch_size: int = 32
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    visual_model: str = "Qwen/Qwen2.5-VL-3B-Instruct"

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
