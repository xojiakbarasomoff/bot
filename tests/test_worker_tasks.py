import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AbstractContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from arq.connections import ArqRedis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import DecryptionError, encrypt
from app.rag.embeddings import EMBEDDING_DIMENSIONS, EmbeddingProvider
from app.rag.llm import ChatMessage, LLMProvider
from app.repositories.channel import ChannelRepository
from app.repositories.knowledge_base import KnowledgeBaseRepository
from app.repositories.tenant import TenantRepository
from app.services.guardrail import EMERGENCY_RESPONSE
from app.services.instagram_client import InstagramClient
from app.workers.tasks import fire_debounce_window, process_inbound_message
from tests.conftest import Seed

QUERY_VECTOR = [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1)


class FakeEmbeddingProvider(EmbeddingProvider):
    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector for _ in texts]


class FakeLLMProvider(LLMProvider):
    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[tuple[str, list[ChatMessage]]] = []

    async def generate(self, system_prompt: str, messages: list[ChatMessage]) -> str:
        self.calls.append((system_prompt, messages))
        return self._reply


class FakeInstagramClient(InstagramClient):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []  # (access_token, recipient_igsid, text)

    async def send_text(self, *, access_token: str, recipient_igsid: str, text: str) -> None:
        self.calls.append((access_token, recipient_igsid, text))


def _session_factory(
    session: AsyncSession,
) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    """Wraps this test's own transactional db_session as the factory
    process_inbound_message calls, instead of it opening a genuinely new
    connection — which couldn't see this test's uncommitted fixture data
    anyway, and would be bound to a different event loop than pytest-asyncio
    hands this test.
    """

    @asynccontextmanager
    async def _factory() -> AsyncIterator[AsyncSession]:
        yield session

    return _factory


async def test_process_inbound_message_runs_under_correct_tenant_and_logs_redacted_reply(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    reply_text = "We're open 9 to 5, Monday to Saturday."
    embedding_provider = FakeEmbeddingProvider(QUERY_VECTOR)
    llm_provider = FakeLLMProvider(reply=reply_text)
    instagram_client = FakeInstagramClient()

    # Seeded (and left) under tenant A's context, then the job is called
    # with NO ambient tenant set — proving it re-establishes tenant context
    # itself from the plain tenant_id argument, rather than relying on one
    # already bound in the caller's context.
    with as_tenant(seed.tenant_a.id):
        await KnowledgeBaseRepository(db_session).create(
            question="What are your hours?", answer="9 to 5, Mon-Sat.", embedding=QUERY_VECTOR
        )

    with caplog.at_level(logging.DEBUG, logger="app.workers.tasks"):
        await process_inbound_message(
            {},
            str(seed.tenant_a.id),
            "sender-1",
            "What time do you open?",
            session_factory=_session_factory(db_session),
            embedding_provider=embedding_provider,
            llm_provider=llm_provider,
            instagram_client=instagram_client,
        )

    [info_record] = [r for r in caplog.records if r.message == "webhook_reply_generated"]
    assert info_record.tenant_id == str(seed.tenant_a.id)  # type: ignore[attr-defined]
    assert info_record.sender_igsid == "sender-1"  # type: ignore[attr-defined]
    assert info_record.reply_length == len(reply_text)  # type: ignore[attr-defined]
    assert info_record.reply_preview == reply_text  # type: ignore[attr-defined]
    # Full reply text must not appear anywhere on the INFO record.
    assert "reply" not in info_record.__dict__

    [debug_record] = [r for r in caplog.records if r.levelname == "DEBUG"]
    assert debug_record.message == "webhook_reply_full_text"
    assert debug_record.reply == reply_text  # type: ignore[attr-defined]

    # The reply was actually sent via the client, to the right recipient,
    # using this tenant's channel access token (seeded as "token" — see
    # tests/conftest.py's seed fixture).
    assert instagram_client.calls == [("token", "sender-1", reply_text)]
    [sent_record] = [r for r in caplog.records if r.message == "instagram_reply_sent"]
    assert sent_record.sender_igsid == "sender-1"  # type: ignore[attr-defined]


async def test_process_inbound_message_reply_preview_is_truncated_for_long_reply(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    reply_text = "A" * 100
    embedding_provider = FakeEmbeddingProvider(QUERY_VECTOR)
    llm_provider = FakeLLMProvider(reply=reply_text)

    with as_tenant(seed.tenant_a.id):
        await KnowledgeBaseRepository(db_session).create(
            question="What are your hours?", answer="9 to 5, Mon-Sat.", embedding=QUERY_VECTOR
        )

    with caplog.at_level(logging.INFO, logger="app.workers.tasks"):
        await process_inbound_message(
            {},
            str(seed.tenant_a.id),
            "sender-1",
            "What time do you open?",
            session_factory=_session_factory(db_session),
            embedding_provider=embedding_provider,
            llm_provider=llm_provider,
            instagram_client=FakeInstagramClient(),
        )

    [info_record] = [r for r in caplog.records if r.message == "webhook_reply_generated"]
    assert info_record.reply_length == 100  # type: ignore[attr-defined]
    assert info_record.reply_preview == "A" * 40 + "…"  # type: ignore[attr-defined]


# --- fire_debounce_window: window fires once after quiet period ---


async def test_fire_debounce_window_processes_joined_batch_when_generation_current(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    redis_pool: ArqRedis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    reply_text = "We're open 9 to 5, Monday to Saturday."
    embedding_provider = FakeEmbeddingProvider(QUERY_VECTOR)
    llm_provider = FakeLLMProvider(reply=reply_text)
    instagram_client = FakeInstagramClient()
    sender_igsid = "sender-1"

    with as_tenant(seed.tenant_a.id):
        await KnowledgeBaseRepository(db_session).create(
            question="What are your hours?", answer="9 to 5, Mon-Sat.", embedding=QUERY_VECTOR
        )

    messages_key = f"debounce:{seed.tenant_a.id}:{sender_igsid}:messages"
    generation_key = f"debounce:{seed.tenant_a.id}:{sender_igsid}:generation"
    await redis_pool.rpush(messages_key, "What time do you open?", "Also, are you open Sundays?")
    await redis_pool.set(generation_key, "3")

    with caplog.at_level(logging.INFO, logger="app.workers.tasks"):
        await fire_debounce_window(
            {"redis": redis_pool},
            str(seed.tenant_a.id),
            sender_igsid,
            3,
            session_factory=_session_factory(db_session),
            embedding_provider=embedding_provider,
            llm_provider=llm_provider,
            instagram_client=instagram_client,
        )

    # The batch was claimed and cleared, generate_answer ran once on the
    # joined text (order preserved), and the reply got logged (redacted).
    assert await redis_pool.exists(messages_key) == 0
    assert await redis_pool.exists(generation_key) == 0
    assert len(llm_provider.calls) == 1
    _system_prompt, messages = llm_provider.calls[0]
    assert messages == [
        {
            "role": "user",
            "content": "What time do you open?\nAlso, are you open Sundays?",
        }
    ]
    [info_record] = [r for r in caplog.records if r.message == "webhook_reply_generated"]
    assert info_record.tenant_id == str(seed.tenant_a.id)  # type: ignore[attr-defined]
    assert info_record.sender_igsid == sender_igsid  # type: ignore[attr-defined]
    assert info_record.reply_preview == reply_text  # type: ignore[attr-defined]

    assert instagram_client.calls == [("token", sender_igsid, reply_text)]


# --- fire_debounce_window: stale generation jobs no-op ---


async def test_fire_debounce_window_stale_generation_does_not_process_or_log(
    seed: Seed,
    redis_pool: ArqRedis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    llm_provider = FakeLLMProvider(reply="should never be used")
    sender_igsid = "sender-1"

    messages_key = f"debounce:{seed.tenant_a.id}:{sender_igsid}:messages"
    generation_key = f"debounce:{seed.tenant_a.id}:{sender_igsid}:generation"
    await redis_pool.rpush(messages_key, "first message")
    await redis_pool.set(generation_key, "2")  # a second message already reset the window

    with caplog.at_level(logging.INFO, logger="app.workers.tasks"):
        await fire_debounce_window(
            {"redis": redis_pool},
            str(seed.tenant_a.id),
            sender_igsid,
            1,  # stale — the live generation is 2
            llm_provider=llm_provider,
        )

    assert llm_provider.calls == []
    assert caplog.records == []
    # Untouched — the still-current generation's buffer must survive so it
    # can fire correctly later.
    messages = await redis_pool.lrange(messages_key, 0, -1)
    assert [m.decode() for m in messages] == ["first message"]
    assert await redis_pool.get(generation_key) == b"2"


# --- Instagram send: 24h messaging window ---


async def test_process_inbound_message_outside_messaging_window_skips_send_and_logs(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    reply_text = "We're open 9 to 5, Monday to Saturday."
    embedding_provider = FakeEmbeddingProvider(QUERY_VECTOR)
    llm_provider = FakeLLMProvider(reply=reply_text)
    instagram_client = FakeInstagramClient()
    stale_last_message_at = datetime.now(UTC) - timedelta(hours=25)

    with as_tenant(seed.tenant_a.id):
        await KnowledgeBaseRepository(db_session).create(
            question="What are your hours?", answer="9 to 5, Mon-Sat.", embedding=QUERY_VECTOR
        )

    with caplog.at_level(logging.INFO, logger="app.workers.tasks"):
        await process_inbound_message(
            {},
            str(seed.tenant_a.id),
            "sender-1",
            "What time do you open?",
            session_factory=_session_factory(db_session),
            embedding_provider=embedding_provider,
            llm_provider=llm_provider,
            instagram_client=instagram_client,
            last_user_message_at=stale_last_message_at,
        )

    # Reply was still generated — only the send is skipped.
    assert instagram_client.calls == []
    [skip_record] = [
        r for r in caplog.records if r.message == "instagram_send_skipped_outside_window"
    ]
    assert skip_record.sender_igsid == "sender-1"  # type: ignore[attr-defined]
    assert skip_record.last_user_message_at == stale_last_message_at.isoformat()  # type: ignore[attr-defined]
    assert skip_record.levelname == "WARNING"


# --- Instagram send: no token configured yet ---


async def test_process_inbound_message_without_configured_token_skips_send_and_logs(
    db_session: AsyncSession,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    embedding_provider = FakeEmbeddingProvider(QUERY_VECTOR)
    llm_provider = FakeLLMProvider(reply="should never be sent")
    instagram_client = FakeInstagramClient()

    # A fresh tenant with a channel whose credentials are still the
    # placeholder sentinel — the real Instagram token is blocked as of
    # writing (see app.services.instagram_client.is_placeholder_credential).
    # Encrypted like any real row (see app.core.encryption): the placeholder
    # gets decrypted back to "" before the placeholder check runs, same as
    # a real token would.
    tenant = await TenantRepository(db_session).create(name="Clinic Unconfigured", status="active")
    with as_tenant(tenant.id):
        channel = await ChannelRepository(db_session).create(
            type="instagram", credentials=encrypt(""), external_id=f"ig-{tenant.id}"
        )

    with caplog.at_level(logging.INFO, logger="app.workers.tasks"):
        await process_inbound_message(
            {},
            str(tenant.id),
            "sender-1",
            "chest pain",  # emergency keyword — skips retrieval/LLM entirely
            session_factory=_session_factory(db_session),
            embedding_provider=embedding_provider,
            llm_provider=llm_provider,
            instagram_client=instagram_client,
        )

    assert instagram_client.calls == []
    [skip_record] = [r for r in caplog.records if r.message == "no_token_configured"]
    assert skip_record.sender_igsid == "sender-1"  # type: ignore[attr-defined]
    assert skip_record.channel_id == str(channel.id)  # type: ignore[attr-defined]
    assert skip_record.levelname == "WARNING"


async def test_process_inbound_message_undecryptable_credentials_raises_not_skips(
    db_session: AsyncSession,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    """A corrupted row or a wrong/rotated ENCRYPTION_KEY must fail the job
    loudly (DecryptionError propagates), not get treated as "no token yet"
    and silently skipped — those are different problems and must not look
    the same in logs/alerts.
    """
    instagram_client = FakeInstagramClient()

    tenant = await TenantRepository(db_session).create(name="Clinic Corrupted", status="active")
    with as_tenant(tenant.id):
        await ChannelRepository(db_session).create(
            type="instagram",
            credentials="not-valid-ciphertext",
            external_id=f"ig-{tenant.id}",
        )

    with pytest.raises(DecryptionError):
        await process_inbound_message(
            {},
            str(tenant.id),
            "sender-1",
            "chest pain",  # emergency keyword — skips retrieval/LLM entirely
            session_factory=_session_factory(db_session),
            instagram_client=instagram_client,
        )

    assert instagram_client.calls == []


# --- Instagram send: emergency replies are sent too ---


async def test_process_inbound_message_emergency_reply_is_sent_via_client(
    db_session: AsyncSession,
    seed: Seed,
    caplog: pytest.LogCaptureFixture,
) -> None:
    instagram_client = FakeInstagramClient()

    with caplog.at_level(logging.INFO, logger="app.workers.tasks"):
        await process_inbound_message(
            {},
            str(seed.tenant_a.id),
            "sender-1",
            "I have severe chest pain",
            session_factory=_session_factory(db_session),
            instagram_client=instagram_client,
        )

    # generate_answer short-circuits emergencies before touching
    # embedding/LLM providers (see app.services.answer.generate_answer), so
    # none were injected above — the fixed EMERGENCY_RESPONSE is what must
    # have been sent.
    assert instagram_client.calls == [("token", "sender-1", EMERGENCY_RESPONSE)]
