from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # AI — Claude (Anthropic)
    anthropic_api_key: str = ""

    # AI — Ollama (local LLMs, per-task model assignment)
    ollama_host: str = "http://100.115.111.9:11434"
    ollama_model_match: str = "qwen3:latest"      # fast JSON scoring, thinking OFF
    ollama_model_resume: str = "qwen3:latest"      # rule-following rewrites, thinking ON
    ollama_model_cover: str = "qwen3:latest"       # prose generation, thinking OFF
    ollama_model_study: str = "qwen2.5:7b"         # fast factual markdown

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id_sai: str = ""
    telegram_chat_id_gf: str = ""

    # Google Sheets
    google_sheets_id_sai: str = ""
    google_sheets_id_gf: str = ""
    google_service_account_json: str = ""

    # Infrastructure
    postgres_url: str = "postgresql://jobseeker:jobseeker123@localhost:5432/jobseeker"
    redis_url: str = "redis://localhost:6379/0"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin123"
    minio_bucket: str = "jobseeker-docs"
    minio_secure: bool = False

    # Temporal
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "jobseeker-queue"

    # Scraper
    scrape_interval_fast: int = 900    # seconds — ATS APIs
    scrape_interval_slow: int = 1800   # seconds — Playwright
    scraper_concurrency: int = 20
    http_timeout: int = 30
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    # Matching
    match_threshold_sai: float = 0.65
    match_threshold_gf: float = 0.60

    # Proxy (optional)
    http_proxy: Optional[str] = None
    https_proxy: Optional[str] = None


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
