from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import URL, make_url


def database_url(value: str | SecretStr) -> URL:
    raw = value.get_secret_value() if isinstance(value, SecretStr) else value
    # Some hosts supply postgres:// and libpq's sslmode instead of asyncpg's ssl.
    if raw.startswith("postgres://"):
        raw = "postgresql://" + raw[len("postgres://") :]
    url = make_url(raw)
    if url.drivername not in {"postgresql", "postgresql+asyncpg"}:
        raise ValueError("Требуется PostgreSQL DATABASE_URL")
    query = dict(url.query)
    if "sslmode" in query:
        query["ssl"] = query.pop("sslmode")
    return url.set(drivername="postgresql+asyncpg", query=query)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: SecretStr
    database_url: SecretStr
    admin_ids: Annotated[frozenset[int], NoDecode]
    auto_approve_enabled: bool = False
    auto_approve_confidence: float = Field(default=0.95, ge=0, le=1)
    max_submissions_per_hour: int = Field(default=3, ge=1, le=100)
    max_text_length: int = Field(default=500, ge=1, le=500)
    log_level: str = "INFO"
    duplicate_window_hours: int = Field(default=24, ge=1)
    draft_ttl_seconds: int = Field(default=1800, ge=60, le=86400)
    retention_days: int = Field(default=180, ge=2)
    original_text_retention_days: int = Field(default=7, ge=1)
    technical_retention_days: int = Field(default=7, ge=1)
    display_timezone: str = "Europe/Moscow"
    channel_id: int | None = None
    publication_enabled: bool = False
    publication_interval_seconds: int = Field(default=60, ge=1, le=86400)

    @field_validator("channel_id", mode="before")
    @classmethod
    def parse_channel_id(cls, value):
        if value is None or value == "":
            return None
        channel_id = int(value)
        if not -(2**63) < channel_id < 0:
            raise ValueError("CHANNEL_ID должен быть отрицательным числовым ID канала")
        return channel_id

    @model_validator(mode="after")
    def validate_publication(self):
        if self.publication_enabled and self.channel_id is None:
            raise ValueError("Для публикации задайте CHANNEL_ID")
        return self

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, value):
        if isinstance(value, str):
            value = value.strip().strip("[]").split(",")
        ids = frozenset(int(str(item).strip()) for item in value if str(item).strip())
        if not ids or any(item <= 0 for item in ids):
            raise ValueError("ADMIN_IDS должен содержать положительные Telegram ID")
        return ids

    @field_validator("database_url")
    @classmethod
    def validate_database(cls, value: SecretStr) -> SecretStr:
        try:
            database_url(value)
        except Exception:
            raise ValueError("Некорректный PostgreSQL DATABASE_URL") from None
        return value

    @field_validator("log_level")
    @classmethod
    def validate_level(cls, value: str) -> str:
        value = value.upper()
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Некорректный LOG_LEVEL")
        return value

    @field_validator("display_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError("Неизвестный DISPLAY_TIMEZONE") from None
        return value
