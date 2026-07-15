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

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()

