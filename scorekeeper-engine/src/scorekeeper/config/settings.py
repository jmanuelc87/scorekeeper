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

    # LLM-as-a-judge configuration (see scorekeeper.core.metrics.judges).
    judge_provider: str = "anthropic"  # "anthropic" | "openai" | "agent"
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    # Override the OpenAI API base URL (e.g. an OpenAI-compatible gateway or a local
    # server such as LM Studio). None uses the SDK default (https://api.openai.com/v1).
    # Consumed by the retrieval parser's direct OpenAI client (see
    # scorekeeper.core.retrieval.parser) and by every OpenAIJudge the factory builds —
    # including the embeddings backend attached to the Anthropic and Agent judges, which
    # is how a key-free agent run keeps its embeddings local.
    openai_base_url: str | None = None
    anthropic_judge_model: str = "claude-sonnet-5"
    openai_judge_model: str = "gpt-5.6-sol"
    judge_max_tokens: int = 16384
    # Per-request timeout (seconds) for every judge LLM call, passed to the provider
    # client. Raised above the SDKs' 600s default because a scoring call with
    # judge_max_tokens output and adaptive thinking can legitimately run long; a
    # timeout here surfaces as a transport error that judge_call names, not a retry
    # (see scorekeeper.core.metrics.judges).
    judge_timeout_seconds: float = 1800.0
    # Claude Agent SDK judge (judge_provider="agent"): runs every judge call through
    # the Claude Code CLI bundled with claude-agent-sdk, so it authenticates with the
    # local Claude Code session and needs no API key. It calls the same Claude models
    # as the Anthropic judge (agent_judge_model must be one of its known models).
    agent_judge_model: str = "claude-sonnet-5"
    # Long-lived Claude Code OAuth token (`claude setup-token`). Outside an interactive
    # session there is no local Claude Code login to authenticate with — a container
    # has no ~/.claude credentials — so the token is what makes judge_provider="agent"
    # usable in Docker. Passed to the CLI subprocess by the judge; None → the CLI falls
    # back to whatever credentials the environment already carries.
    claude_code_oauth_token: str | None = None
    # Per-step model overrides. Metrics label each judge call with a JudgeStep and
    # these route that step to a specific model of the active provider, so bulk
    # extraction (EXTRACT) can run on a cheaper/faster model than the decisive rubric
    # scoring (SCORE). Unset (None) → the provider's default judge model above (see
    # scorekeeper.core.metrics.judges). There is deliberately no VERIFY override: the only
    # per-item verification (faithfulness) pins its own models per instance instead of
    # routing through the judge's step config, so a VERIFY knob would be a no-op.
    judge_extract_model: str | None = None
    judge_score_model: str | None = None
    # Embeddings for similarity-based metrics (e.g. answer relevance). Neither
    # Anthropic nor the Agent SDK offers an embedding endpoint, so embeddings always
    # run through an OpenAI-compatible one (see openai_base_url); with either of those
    # providers an OpenAI-backed embedder is attached iff openai_api_key is set (see
    # scorekeeper.core.metrics.judges.make_judge).
    openai_embedding_model: str = "text-embedding-3-small"
    # Retrieval embedding phase (scorekeeper.core.services.embedding): a document's
    # sentences are grouped into overlapping chunks, each chunk is embedded, and at
    # scoring time only the chunks most similar to the turn's prompt are rendered into
    # the judge prompt. The embedder is its own OpenAI client, independent of the judge.
    embedding_chunk_sentences: int = 5  # sentences per chunk
    embedding_chunk_overlap: int = 1  # sentences shared with the previous chunk
    embedding_batch_size: int = 128  # texts per embeddings API call
    embedding_top_k: int = 3  # chunks per document handed to the judge
    # Server-side refusal fallback (scorekeeper.core.metrics.judges.anthropic_judge). A
    # safety classifier may decline a judge call (HTTP 200, stop_reason="refusal"); with
    # this set, the API re-runs that same call on this model inside the same request
    # instead of leaving the turn unscored. It only applies to the Claude 5 models that
    # support the feature, so it is inert while the judge runs on Opus 4.8. The model
    # must be one the Anthropic judge owns. None disables the parameter entirely.
    judge_fallback_model: str | None = "claude-opus-5"
    # Model pins for the faithfulness_ragas entailment cascade: a cheap high-volume model
    # decides every claim, and only verdicts it reports below escalation_confidence are
    # re-judged by the decisive one. Both must be models the Anthropic judge owns.
    faithfulness_bulk_model: str = "claude-sonnet-5"
    faithfulness_audit_model: str = "claude-opus-5"
    # Override the judge system prompt at runtime; None uses the built-in default.
    judge_system_prompt: str | None = None
    # Trace every LLM API call the judge makes (op, model, step, turn, sizes,
    # latency, outcome) to the ``scorekeeper.core.metrics.judges.tracing`` logger. Purely
    # observational — wraps the judge in a TracingJudge and never alters scoring.
    judge_trace_enabled: bool = False

    # Local filesystem directory for the retrieval pipeline's fetch cache. Fetched document
    # bytes are stored here (indexed by the ``document_cache`` table) so a document is
    # downloaded at most once per platform execution (see scorekeeper.core.retrieval.fetch). The
    # entries are purged when that execution's retrieval finishes, so the directory does not
    # accumulate documents across runs (see evaluation.retrieve_run).
    retrieval_cache_dir: str = "./retrieval-cache"

    # Extract stage backend (scorekeeper.core.retrieval.extract.default_content_extractor).
    # "markdown" converts with MarkItDown in-process; "unstructured" posts the document to
    # the unstructured-api service (compose.yaml), which returns typed elements carrying a
    # page number and, for a table, its HTML. The unstructured path needs
    # ``unstructured_api_url``; without it the markdown extractor is used instead, so a
    # misconfigured deployment degrades rather than failing every retrieval.
    retrieval_extractor: str = "markdown"  # "markdown" | "unstructured"
    unstructured_api_url: str | None = None  # e.g. http://unstructured-api:8000
    unstructured_api_key: str | None = None  # hosted SaaS only; self-hosting needs none
    # Base partition strategy: "fast" reads the PDF text layer, "hi_res" runs the layout
    # model and OCR (far slower, and the only strategy that recovers table structure well).
    unstructured_strategy: str = "fast"
    # Re-post a PDF page whose text layer came back empty with strategy="hi_res". This is
    # what makes a scanned document readable instead of EMPTY_CONTENT. Best-effort: an OCR
    # failure leaves the page empty rather than failing the document.
    unstructured_ocr_fallback: bool = True
    unstructured_ocr_languages: str = "spa"  # comma-separated tesseract languages
    unstructured_timeout_seconds: float = 300.0  # one hi_res page is slow
    # Above this many text-less pages the OCR fallback is skipped wholesale: a fully scanned
    # 300-page PDF would otherwise occupy a worker for an hour to no one's benefit.
    unstructured_max_ocr_pages: int = 20
    # A rendered table is one chunk, never split mid-grid — but one chunk is also one
    # embedding input, and an oversized one fails the whole batch. Past this many characters
    # a table is split by rows, each piece repeating the header. 0 disables the split.
    extract_table_max_chars: int = 4000

    # Master secret for the retrieval pipeline's credential store. Certificate private
    # keys configured per provider (the ``auth_providers`` table) are stored encrypted:
    # a per-row salt derives a key from this secret (PBKDF2) to encrypt/decrypt the PEM
    # (see scorekeeper.core.retrieval.credentials.secrets). Unset (None) → SharePoint providers
    # cannot build a client and gated documents resolve to MISSING_CREDENTIALS.
    auth_encryption_key: str | None = None

    # Pace scoring by pausing a random interval (seconds) between consecutive turns
    # of a scenario, to spread judge calls out over time. The pause is drawn
    # uniformly from [turn_delay_min_seconds, turn_delay_max_seconds]; set both to 0
    # to disable.
    turn_delay_min_seconds: float = 0.5
    turn_delay_max_seconds: float = 2.0

    # Backoff for judge calls the provider throttles or sheds (HTTP 429/503/529). The
    # pause above is open-loop and per-process, so it cannot bound the aggregate request
    # rate once a run is spread across workers, turns and concurrent metrics; this is the
    # closed loop that can, since each caller reacts to the throttling it actually sees
    # (see scorekeeper.core.metrics.judges.base.judge_call). Attempts count the first try,
    # so 1 disables retrying. The wait is exponential with full jitter — jitter is what
    # keeps concurrent callers from re-firing in lockstep — unless the provider sent a
    # Retry-After, which wins; either way it is clamped to judge_retry_max_seconds.
    judge_retry_max_attempts: int = 5
    judge_retry_base_seconds: float = 1.0
    judge_retry_max_seconds: float = 60.0

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("judge_system_prompt", mode="after")
    @classmethod
    def _blank_prompt_is_none(cls, value: str | None) -> str | None:
        # A blank JUDGE_SYSTEM_PROMPT (e.g. the empty line in .env.example) means
        # "unset" — fall back to the judge's built-in default, not an empty prompt.
        if value is not None and not value.strip():
            return None
        return value

    @field_validator("claude_code_oauth_token", mode="after")
    @classmethod
    def _blank_token_is_none(cls, value: str | None) -> str | None:
        # A blank CLAUDE_CODE_OAUTH_TOKEN (the empty line in .env.example) means
        # "unset": the judge must leave the variable alone rather than hand the CLI an
        # empty token, which would shadow any credentials it could otherwise use.
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

