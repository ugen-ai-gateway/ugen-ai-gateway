from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "UGen AI Gateway"
    app_env: str = "development"

    local_api_key: str = "local"
    local_base_url: str = ""
    local_model: str = ""
    # Optional public names for the models: shown in the dashboard and API
    # responses instead of the real model names, and accepted as `model`.
    local_model_alias: str = ""
    # Model for /v1/embeddings, always on the local server (Models page).
    embedding_model: str = ""

    cloud_api_key: str = ""
    cloud_base_url: str = ""
    cloud_model: str = ""
    cloud_model_alias: str = ""

    # Optional YAML file with providers, models and routing rules. When empty,
    # LOCAL_* / CLOUD_* define a local and a cloud provider.
    routing_config: str = ""

    # Optional name/location detection: none or presidio (uv sync --extra ner).
    pii_ner: Literal["none", "presidio"] = "none"
    pii_ner_language: str = "zh"
    pii_ner_spacy_model: str = "zh_core_web_sm"
    pii_ner_score_threshold: float = Field(default=0.5, ge=0, le=1)

    # llm: always ask the local model. rules: regular expressions only.
    # hybrid: rules when the request stays local anyway, the local model otherwise.
    analysis_mode: Literal["llm", "rules", "hybrid"] = "hybrid"
    # The lowest difficulty sent to the cloud model in Auto mode.
    cloud_from: Literal["moderate", "complex"] = "moderate"
    analysis_cache_size: int = Field(default=256, ge=0)
    analysis_cache_ttl_seconds: float = Field(default=600, gt=0)

    # Prompt-injection tripwire: off, flag (record only), local (never send to
    # the cloud), or block (reject with HTTP 400).
    injection_action: Literal["off", "flag", "local", "block"] = "flag"
    # Personal data in model output that the user never provided: off, flag
    # (record only), or redact (replace with a [LABEL] marker).
    output_pii_action: Literal["off", "flag", "redact"] = "off"
    # Required for the audit endpoints when set; otherwise GATEWAY_API_KEY rules.
    admin_api_key: str = ""

    # SQLite file for request metadata and feedback (never message text).
    # Empty disables usage tracking.
    usage_db_path: str = "data/ugen.db"

    gateway_api_key: str = ""
    # Encrypts provider API keys stored in the database. Empty: a key file is
    # generated next to the database (secret.key); back it up with it.
    secret_key: str = ""
    # Dashboard accounts. The first account is always allowed and becomes the
    # administrator; set ALLOW_REGISTRATION=false to stop further sign-ups.
    allow_registration: bool = True
    session_ttl_days: float = Field(default=30, gt=0)
    # Saved conversations per account, measured compressed and encrypted.
    conversation_storage_mb: float = Field(default=200, gt=0)
    # Built dashboard (frontend/dist) served at "/". Empty: use frontend/dist
    # next to the source tree when it exists.
    static_dir: str = ""
    rate_limit_requests: int = Field(default=60, gt=0)
    rate_limit_window_seconds: int = Field(default=60, gt=0)
    request_timeout: float = Field(default=300.0, gt=0, le=600)
    # Retries with exponential backoff on connection errors, 429 and 5xx.
    max_retries: int = Field(default=2, ge=0, le=10)

    # Automatic fallback when the routed provider is down (auto routing only).
    # Cloud -> local is privacy-safe. Local -> cloud only happens for requests
    # the local model analyzed and found free of personal data.
    fallback_to_local: bool = True
    fallback_to_cloud: bool = False
    circuit_failure_threshold: int = Field(default=3, ge=1)
    circuit_cooldown_seconds: float = Field(default=30, gt=0)

    @model_validator(mode="after")
    def validate_runtime_security(self) -> "Settings":
        if self.app_env.casefold() == "production" and not self.gateway_api_key:
            raise ValueError("GATEWAY_API_KEY is required in production")
        return self

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8",
        # .env also holds values for other tools (GATEWAY_PORT for Docker).
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
