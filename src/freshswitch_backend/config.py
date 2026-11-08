from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    data_go_kr_service_key: str = ""
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model_name: str = ""
    cors_origins: list[str] = ["*"]
    explain_use_llm: bool = False
    offline_mode: bool = False
    replan_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    explain_timeout_seconds: float = Field(default=8.0, gt=0, le=10)
    demo_reset_token: SecretStr = SecretStr("")
    database_url: str = ""
    postgres_host: str = ""
    postgres_port: int = 5432
    postgres_user: str = "freshswitch"
    postgres_password: str = "freshswitch"
    postgres_db: str = "freshswitch"

    def resolved_database_url(self) -> str:
        if self.database_url or not self.postgres_host:
            return self.database_url
        return URL.create(
            "postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        ).render_as_string(hide_password=False)


@lru_cache
def get_settings() -> Settings:
    return Settings()
