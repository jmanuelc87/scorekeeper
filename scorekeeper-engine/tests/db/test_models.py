"""Tests for ORM-level helpers in ``scorekeeper.db.models``."""

from __future__ import annotations

import pytest

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.db.models import (
    AuthProviderConfig,
    BenchmarkRun,
    MetricDefinition,
    PlatformExecution,
    Prompt,
    PromptVersion,
    RetrievedContextDocument,
    RetrievedDocumentEmbedding,
    ScenarioResult,
    Turn,
)
from scorekeeper.core.retrieval.credentials.secrets import SecretError
from scorekeeper.core.retrieved_context import RetrievedDocument


def test_from_document_maps_fields_and_rank() -> None:
    doc = RetrievedDocument(name="n", document="d.pdf", content="c", url="http://x")
    row = RetrievedContextDocument.from_document(doc, rank=3)
    assert (row.rank, row.name, row.document, row.content, row.url) == (
        3,
        "n",
        "d.pdf",
        "c",
        "http://x",
    )


def test_to_document_round_trips() -> None:
    doc = RetrievedDocument(name="n", document="d.pdf", content="c", url=None)
    assert RetrievedContextDocument.from_document(doc, rank=0).to_document() == doc


def test_auth_provider_from_sharepoint_encrypts_and_round_trips() -> None:
    key = "clave-maestra"
    row = AuthProviderConfig.from_sharepoint(
        host="h.sharepoint.com",
        tenant_id="t",
        client_id="c",
        thumbprint="th",
        site_url="https://h.sharepoint.com/sites/x",
        private_key="-----BEGIN PRIVATE KEY-----secret",
        encryption_key=key,
    )
    # The PEM is never stored in the clear; only the encrypted token + salt are.
    assert row.provider == "sharepoint"
    assert row.private_key_encrypted and row.private_key_salt
    assert "secret" not in (row.private_key_encrypted or "")
    assert row.decrypted_private_key(key) == "-----BEGIN PRIVATE KEY-----secret"


def test_auth_provider_decrypt_wrong_key_raises() -> None:
    row = AuthProviderConfig.from_sharepoint(
        host="h.sharepoint.com",
        tenant_id="t",
        client_id="c",
        thumbprint="th",
        site_url="https://h.sharepoint.com/sites/x",
        private_key="pem",
        encryption_key="clave-1",
    )
    with pytest.raises(SecretError):
        row.decrypted_private_key("clave-2")


def test_auth_provider_decrypt_without_stored_key_raises() -> None:
    with pytest.raises(SecretError):
        AuthProviderConfig(provider="sharepoint", host="h").decrypted_private_key("k")


def test_auth_provider_from_oauth2_encrypts_secret_and_stores_settings() -> None:
    key = "clave-maestra"
    row = AuthProviderConfig.from_oauth2(
        host="api.example.com",
        client_id="cid",
        client_secret="s3cr3t",
        token_url="https://idp/token",
        scope="files.read",
        encryption_key=key,
    )
    assert row.provider == "oauth2"
    # Non-secret OAuth2 config lands in ``settings``; the secret is encrypted.
    assert row.settings == {"token_url": "https://idp/token", "scope": "files.read"}
    assert "s3cr3t" not in (row.private_key_encrypted or "")
    # ``decrypted_secret`` is the generic accessor; the cert alias returns the same value.
    assert row.decrypted_secret(key) == "s3cr3t"
    assert row.decrypted_private_key(key) == "s3cr3t"


# --- prompt catalog constraints ----------------------------------------------
# These run against the SQLite fixture, which is the point: the two partial unique
# indexes declare both ``postgresql_where`` and ``sqlite_where``, so ``create_all``
# here enforces exactly what Alembic creates on PostgreSQL.


async def _a_prompt(session: AsyncSession) -> Prompt:
    metric = MetricDefinition(name="metrica")
    session.add(metric)
    await session.flush()
    prompt = Prompt(metric_id=metric.id, slug="verify", required_variables=["claim"])
    session.add(prompt)
    await session.flush()
    return prompt


def _version(prompt: Prompt, version: int, status: str, *, is_active: bool = False) -> PromptVersion:
    return PromptVersion(
        prompt_id=prompt.id,
        version=version,
        template="Afirmación: {claim}",
        status=status,
        is_active=is_active,
    )


async def test_a_metric_cannot_declare_the_same_slug_twice(session: AsyncSession) -> None:
    prompt = await _a_prompt(session)
    session.add(Prompt(metric_id=prompt.metric_id, slug="verify"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_two_metrics_may_share_a_slug(session: AsyncSession) -> None:
    """``verify`` belongs to both faithfulness metrics; uniqueness is per metric."""
    await _a_prompt(session)
    other = MetricDefinition(name="otra")
    session.add(other)
    await session.flush()
    session.add(Prompt(metric_id=other.id, slug="verify"))
    await session.flush()  # does not raise


async def test_only_one_published_version_may_be_active(session: AsyncSession) -> None:
    prompt = await _a_prompt(session)
    session.add(_version(prompt, 1, "published", is_active=True))
    await session.flush()
    session.add(_version(prompt, 2, "published", is_active=True))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_inactive_published_versions_may_pile_up(session: AsyncSession) -> None:
    """Only the *active* one is unique — superseded published versions are kept forever."""
    prompt = await _a_prompt(session)
    session.add(_version(prompt, 1, "published"))
    session.add(_version(prompt, 2, "published"))
    session.add(_version(prompt, 3, "published", is_active=True))
    await session.flush()  # does not raise


async def test_only_one_draft_may_be_open(session: AsyncSession) -> None:
    """A further edit supersedes the previous draft rather than opening a second one."""
    prompt = await _a_prompt(session)
    session.add(_version(prompt, 1, "draft"))
    await session.flush()
    session.add(_version(prompt, 2, "draft"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_discarded_drafts_do_not_block_a_new_one(session: AsyncSession) -> None:
    prompt = await _a_prompt(session)
    session.add(_version(prompt, 1, "discarded"))
    session.add(_version(prompt, 2, "discarded"))
    session.add(_version(prompt, 3, "draft"))
    await session.flush()  # does not raise


async def test_a_draft_cannot_be_active(session: AsyncSession) -> None:
    """The check constraint: nothing scores under an unpublished version."""
    prompt = await _a_prompt(session)
    session.add(_version(prompt, 1, "draft", is_active=True))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_version_numbers_are_unique_per_prompt(session: AsyncSession) -> None:
    prompt = await _a_prompt(session)
    session.add(_version(prompt, 1, "published"))
    await session.flush()
    session.add(_version(prompt, 1, "discarded"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_deleting_a_run_cascades_through_the_whole_tree(
    session: AsyncSession, compose_use_case
) -> None:
    """The inverted hierarchy cascades all the way down from the run.

    Neither the executions nor the turns hang off the run directly any more, so the
    delete has to travel run → scenario → platform execution → turn to reach them.
    """
    run = BenchmarkRun()
    scenario = ScenarioResult(
        scenario_id="esc-1", use_case=await compose_use_case([]), run=run
    )
    # Two platforms answering one scenario, each with its own turns.
    for platform in ("claude", "gemini"):
        PlatformExecution(platform=platform, scenario_result=scenario).turns.append(
            Turn(turn_number=1, prompt="p", response="r")
        )
    session.add(scenario)
    await session.commit()

    await session.delete(run)
    await session.commit()

    for model in (ScenarioResult, PlatformExecution, Turn):
        remaining = (await session.execute(select(func.count()).select_from(model))).scalar_one()
        assert remaining == 0, model.__name__


async def test_deleting_a_run_cascades_into_document_embeddings(
    session: AsyncSession, compose_use_case
) -> None:
    """Embeddings hang two levels below the turn and must still go with the run.

    The chain is run → scenario → platform execution → turn → retrieved document →
    embedding, so this covers the two links the tree-wide test above does not reach.
    """
    run = BenchmarkRun()
    scenario = ScenarioResult(
        scenario_id="esc-1", use_case=await compose_use_case([]), run=run
    )
    turn = Turn(turn_number=1, prompt="p", response="r")
    PlatformExecution(platform="claude", scenario_result=scenario).turns.append(turn)
    document = RetrievedContextDocument(
        rank=0, name="doc", document="d.pdf", content="uno dos", url=None
    )
    turn.retrieved_documents.append(document)
    # Two chunks of the one document. The vectors are short on purpose: this asserts the
    # FK chain, and on the SQLite fallback the column is JSON and enforces no width.
    for index, chunk in enumerate(("uno", "dos")):
        document.embeddings.append(
            RetrievedDocumentEmbedding(
                chunk_index=index, content=chunk, embedding=[0.1, 0.2, 0.3]
            )
        )
    session.add(scenario)
    await session.commit()

    stored = (
        await session.execute(select(func.count()).select_from(RetrievedDocumentEmbedding))
    ).scalar_one()
    assert stored == 2

    await session.delete(run)
    await session.commit()

    for model in (RetrievedContextDocument, RetrievedDocumentEmbedding):
        remaining = (await session.execute(select(func.count()).select_from(model))).scalar_one()
        assert remaining == 0, model.__name__


async def test_document_embedding_chunk_index_is_unique_per_document(
    session: AsyncSession, compose_use_case
) -> None:
    """Re-embedding a document replaces its chunks rather than accumulating them."""
    run = BenchmarkRun()
    scenario = ScenarioResult(
        scenario_id="esc-1", use_case=await compose_use_case([]), run=run
    )
    turn = Turn(turn_number=1, prompt="p", response="r")
    PlatformExecution(platform="claude", scenario_result=scenario).turns.append(turn)
    document = RetrievedContextDocument(
        rank=0, name="doc", document="d.pdf", content="uno", url=None
    )
    turn.retrieved_documents.append(document)
    document.embeddings.append(
        RetrievedDocumentEmbedding(chunk_index=0, content="uno", embedding=[0.1])
    )
    session.add(scenario)
    await session.flush()

    document.embeddings.append(
        RetrievedDocumentEmbedding(chunk_index=0, content="duplicado", embedding=[0.2])
    )
    with pytest.raises(IntegrityError):
        await session.flush()
