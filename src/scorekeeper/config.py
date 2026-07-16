from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./scorekeeper.db"
    mcp_transport: str = "stdio"
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000
    api_host: str = "0.0.0.0"
    api_port: int = 8001
    cors_origins: str = "http://localhost:5173,http://localhost:8080"

    # Celery broker for the evaluation worker. Defaults to the app's own Postgres
    # via kombu's SQLAlchemy transport (see the ``broker_url`` property); set
    # CELERY_BROKER_URL to point at a dedicated broker (e.g. Redis) instead.
    celery_broker_url: str | None = None

    # LLM-as-a-judge configuration (see scorekeeper.metrics.judges).
    judge_provider: str = "anthropic"  # "anthropic" | "openai"
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    anthropic_judge_model: str = "claude-opus-4-8"
    openai_judge_model: str = "gpt-5.6-sol"
    judge_max_tokens: int = 16384
    # Embeddings for similarity-based metrics (e.g. answer relevance). Anthropic
    # offers no embedding endpoint, so embeddings always run through OpenAI; when
    # the judge provider is Anthropic, an OpenAI-backed embedder is attached iff
    # openai_api_key is set (see scorekeeper.metrics.judges.make_judge).
    openai_embedding_model: str = "text-embedding-3-small"
    # Override the judge system prompt at runtime; None uses the built-in default.
    judge_system_prompt: str | None = None

    # Pace scoring by pausing a random interval (seconds) between consecutive turns
    # of a scenario, to spread judge calls out over time. The pause is drawn
    # uniformly from [turn_delay_min_seconds, turn_delay_max_seconds]; set both to 0
    # to disable.
    turn_delay_min_seconds: float = 0.5
    turn_delay_max_seconds: float = 2.0

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def broker_url(self) -> str:
        # kombu's SQLAlchemy transport is the app's DB URL with an "sqla+" prefix.
        return self.celery_broker_url or f"sqla+{self.database_url}"


@lru_cache
def get_settings() -> Settings:
    return Settings()

