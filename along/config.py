from pathlib import Path
from typing import Literal
import sys

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_dir() -> Path:
    root = Path.home() / ("Library/Application Support" if sys.platform == "darwin" else ".local/share")
    return root / "ALONG"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ALONG_", env_file=".env", extra="ignore")
    data_dir: Path = Field(default_factory=default_data_dir)
    llm_provider: str = "deepseek"
    proxy_url: str = "http://127.0.0.1:8091/v1/responses"
    model: str = "deepseek-v4.1-flash"
    deepseek_api_key: SecretStr | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    deepseek_reasoning_effort: Literal["low", "high", "max"] = "low"
    deepseek_max_tokens: int = Field(default=16384, ge=1024, le=65536)
    port: int = 8766
    request_timeout: float = 90
    event_window: int = Field(default=10, ge=10, le=300)
    event_frames: int = Field(default=10, ge=2, le=24)
    event_overlap_frames: int = Field(default=2, ge=0, le=4)
    frame_interval: int = Field(default=1, ge=1, le=30)
    max_storage_mb: int = Field(default=4096, ge=100)
    retention_days: int = Field(default=7, ge=1)
    max_images: int = Field(default=12, ge=2, le=24)
    memory_rounds: int = Field(default=2, ge=1, le=3)
    memory_recent_seconds: int = Field(default=30, ge=10, le=120)
    memory_results: int = Field(default=5, ge=1, le=10)
    clip_frames: int = Field(default=8, ge=2, le=12)
    sheet_frames: int = Field(default=12, ge=4, le=12)
    clip_seconds: int = Field(default=8, ge=2, le=120)
    memory_candidates: int = Field(default=512, ge=40, le=4096)
    memory_catalog_chars: int = Field(default=24000, ge=4000, le=100000)
    agent_timeout: float = Field(default=100, ge=10, le=110)
    task_answer_timeout: float = Field(default=12, ge=10, le=60)
    speech_provider: Literal["siliconflow", "local_whisper"] = "siliconflow"
    siliconflow_api_key: SecretStr | None = None
    siliconflow_asr_model: str = "FunAudioLLM/SenseVoiceSmall"
    speech_timeout: float = Field(default=60, ge=5, le=90)
    whisper_model: str = "small"
    semantic_enabled: bool = False
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    worker_enabled: bool = True
