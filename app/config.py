"""Настройки сервиса: только из переменных окружения (.env на сервере, в репозитории .env.example)."""
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://postgres@localhost:5432/scorecast"
    public_url: str = "http://localhost:8000"
    tz_name: str = Field("Europe/Moscow", alias="TZ")

    games_per_run: int = 20
    crawl_cron_minute: int = 0
    request_delay_sec: float = 1.0
    critic_reviews_max: int = 30
    user_reviews_max: int = 50
    max_see_all_pages: int = 10

    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4.1-mini"
    llm_price_in: float = 0.40    # $ за 1M входных токенов
    llm_price_out: float = 1.60   # $ за 1M выходных токенов
    llm_workers: int = 2

    embedding_model: str = ""     # пусто: похожие считаются только по признакам игры
    embedding_price: float = 0.02

    letsplay_enabled: bool = True

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.tz_name)

    @property
    def dsn(self) -> str:
        # DATABASE_URL может прийти в формате SQLAlchemy (postgresql+asyncpg://), psycopg нужен обычный
        return self.database_url.replace("+asyncpg", "").replace("+psycopg", "")

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def embeddings_enabled(self) -> bool:
        return bool(self.llm_api_key and self.embedding_model)


settings = Settings()
