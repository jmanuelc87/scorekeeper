from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Async driver -> the sync equivalent kombu's SQLAlchemy broker transport needs. The app
# itself always speaks the async driver; only Celery's broker takes the sync detour (see
# ``Settings.sync_database_url``), so one DATABASE_URL stays the single source of truth.
_SYNC_SCHEMES = {
    "postgresql+asyncpg": "postgresql+psycopg",
    "sqlite+aiosqlite": "sqlite",
}


class Settings(BaseSettings):
    # Must name an *async* SQLAlchemy driver — the engine is a create_async_engine.
    # Note asyncpg does not speak libpq query parameters: "?sslmode=require" is silently
    # ignored (TLS goes through connect_args={"ssl": ...}), and a bare "postgres://"
    # scheme is rejected.
    database_url: str = "sqlite+aiosqlite:///./scorekeeper.db"
    api_host: str = "0.0.0.0"
    api_port: int = 8001
    cors_origins: str = "http://localhost:5173,http://localhost:8080"

    # Root log level for the structlog plain-text output (single stream on stdout).
    # See scorekeeper.utils.logging_config.configure_logging.
    log_level: str = "INFO"

    # Celery broker for the evaluation worker. Defaults to the app's own Postgres
    # via kombu's SQLAlchemy transport (see the ``broker_url`` property); set
    # CELERY_BROKER_URL to point at a dedicated broker (e.g. Redis) instead.
    celery_broker_url: str | None = None

    # LLM-as-a-judge configuration (see scorekeeper.metrics.judges).
    judge_provider: str = "anthropic"  # "anthropic" | "openai" | "lmstudio"
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    # Override the OpenAI API base URL (e.g. an OpenAI-compatible gateway or a local
    # proxy). None uses the SDK default (https://api.openai.com/v1). Consumed by the
    # retrieval parser's direct OpenAI client (see scorekeeper.retrieval.parser).
    openai_base_url: str | None = None
    anthropic_judge_model: str = "claude-opus-4-8"
    openai_judge_model: str = "gpt-5.6-sol"
    judge_max_tokens: int = 16384
    # Local LM Studio judge (judge_provider="lmstudio"): an OpenAI-compatible server
    # for end-to-end testing with no API key or external cost. It remaps every
    # requested/pinned model to lmstudio_judge_model (the one loaded model), so all
    # metrics run against whatever LM Studio has loaded. answer_relevance also needs
    # an embedding model loaded (lmstudio_embedding_model).
    lmstudio_base_url: str = "http://localhost:1234/v1"
    lmstudio_judge_model: str = "local-model"  # set to the exact id shown in LM Studio
    lmstudio_api_key: str = "lm-studio"  # ignored by LM Studio; the SDK needs non-empty
    lmstudio_embedding_model: str = "text-embedding-nomic-embed-text-v1.5"
    # Per-step model overrides. Metrics label each judge call with a JudgeStep and
    # these route that step to a specific model of the active provider, so bulk
    # extraction (EXTRACT) can run on a cheaper/faster model than the decisive rubric
    # scoring (SCORE). Unset (None) → the provider's default judge model above (see
    # scorekeeper.metrics.judges). There is deliberately no VERIFY override: the only
    # per-item verification (faithfulness) pins its own models per instance instead of
    # routing through the judge's step config, so a VERIFY knob would be a no-op.
    judge_extract_model: str | None = None
    judge_score_model: str | None = None
    # Embeddings for similarity-based metrics (e.g. answer relevance). Anthropic
    # offers no embedding endpoint, so embeddings always run through OpenAI; when
    # the judge provider is Anthropic, an OpenAI-backed embedder is attached iff
    # openai_api_key is set (see scorekeeper.metrics.judges.make_judge).
    openai_embedding_model: str = "text-embedding-3-small"
    # Override the judge system prompt at runtime; None uses the built-in default.
    judge_system_prompt: str | None = None
    # Trace every LLM API call the judge makes (op, model, step, turn, sizes,
    # latency, outcome) to the ``scorekeeper.metrics.judges.tracing`` logger. Purely
    # observational — wraps the judge in a TracingJudge and never alters scoring.
    judge_trace_enabled: bool = False

    # Local filesystem directory for the retrieval pipeline's fetch cache. Fetched document
    # bytes are stored here (indexed by the ``document_cache`` table) so a document is
    # downloaded at most once per platform execution (see scorekeeper.retrieval.fetch). The
    # entries are purged when that execution's retrieval finishes, so the directory does not
    # accumulate documents across runs (see evaluation.retrieve_run).
    retrieval_cache_dir: str = "./retrieval-cache"

    # Master secret for the retrieval pipeline's credential store. Certificate private
    # keys configured per provider (the ``auth_providers`` table) are stored encrypted:
    # a per-row salt derives a key from this secret (PBKDF2) to encrypt/decrypt the PEM
    # (see scorekeeper.retrieval.credentials.secrets). Unset (None) → SharePoint providers
    # cannot build a client and gated documents resolve to MISSING_CREDENTIALS.
    auth_encryption_key: str | None = None

    # Pace scoring by pausing a random interval (seconds) between consecutive turns
    # of a scenario, to spread judge calls out over time. The pause is drawn
    # uniformly from [turn_delay_min_seconds, turn_delay_max_seconds]; set both to 0
    # to disable.
    turn_delay_min_seconds: float = 0.5
    turn_delay_max_seconds: float = 2.0

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("judge_system_prompt", mode="after")
    @classmethod
    def _blank_prompt_is_none(cls, value: str | None) -> str | None:
        # A blank JUDGE_SYSTEM_PROMPT (e.g. the empty line in .env.example) means
        # "unset" — fall back to the judge's built-in default, not an empty prompt.
        if value is not None and not value.strip():
            return None
        return value

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def sync_database_url(self) -> str:
        """``database_url`` with its async driver swapped for the sync equivalent."""
        scheme, sep, rest = self.database_url.partition("://")
        return f"{_SYNC_SCHEMES.get(scheme, scheme)}{sep}{rest}"

    @property
    def broker_url(self) -> str:
        # kombu's SQLAlchemy transport is a *sync* DBAPI URL with an "sqla+" prefix — it
        # cannot drive asyncpg, hence sync_database_url rather than database_url.
        return self.celery_broker_url or f"sqla+{self.sync_database_url}"


@lru_cache
def get_settings() -> Settings:
    return Settings()

