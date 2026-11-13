from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./training.db"
    jwt_secret: str = Field(min_length=32)
    environment: str = "development"
    access_minutes: int = Field(default=30, ge=1, le=120)
    refresh_days: int = Field(default=30, ge=1, le=90)
    cors_origins: list[str] = []
    notification_threshold: int = Field(default=2, ge=0, le=100)

    @model_validator(mode="after")
    def production(self):
        if self.environment == "production" and not self.database_url.startswith("mysql+pymysql://"):
            raise ValueError("Production requires MySQL with the PyMySQL driver")
        return self


@lru_cache
def settings():
    return Settings()
