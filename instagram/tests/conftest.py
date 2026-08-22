from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import pytest
from arq.connections import ArqRedis
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.encryption import encrypt
from app.core.passwords import hash_password
from app.core.tenant_context import reset_current_tenant, set_current_tenant
from app.models.appointment import Appointment, AppointmentStatus
from app.models.channel import Channel
from app.models.conversation import Conversation
from app.models.doctor import Doctor
from app.models.knowledge_base import KnowledgeBase
from app.models.lead import Lead
from app.models.message import Message
from app.models.operator import Operator
from app.models.tenant import Tenant
from app.models.user import User
from app.rag.embeddings import EMBEDDING_DIMENSIONS
from app.repositories.appointment import AppointmentRepository
from app.repositories.channel import ChannelRepository
from app.repositories.conversation import ConversationRepository
from app.repositories.doctor import DoctorRepository
from app.repositories.knowledge_base import KnowledgeBaseRepository
from app.repositories.lead import LeadRepository
from app.repositories.message import MessageRepository
from app.repositories.operator import OperatorRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """Wraps each test in an outer transaction (with a SAVEPOINT for any inner
    flush/commit) that's always rolled back afterward, so tests never leave
    data behind and never see another test's writes. Requires the Dockerized
    Postgres from docker-compose.yml to be reachable at DATABASE_URL.

    expire_on_commit=False matches app.core.db's real sessionmaker: without
    it, a route that calls session.commit() (e.g. the dashboard's
    create/cancel endpoints) would expire every ORM object already loaded in
    this shared test session — including the `seed` fixture's rows — and
    the next plain attribute access on one of them would need an implicit
    reload that isn't safe outside an awaited call.
    """
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    async with engine.connect() as connection:
        trans = await connection.begin()
        session = AsyncSession(
            bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        try:
            yield session
        finally:
            await session.close()
            await trans.rollback()
    await engine.dispose()


# Dedicated Redis DB index for tests — never db 0 (dev/prod), so a blanket
# flushdb() before/after each test can't ever touch real data.
TEST_REDIS_URL = "redis://localhost:6379/15"


@pytest.fixture
async def redis_pool() -> AsyncIterator[ArqRedis]:
    """A real ArqRedis pool against a dedicated test DB, flushed clean
    before and after each test. Debounce logic relies on real Redis Lua
    scripts and list/counter semantics that aren't meaningfully fakeable.
    """
    pool = ArqRedis.from_url(TEST_REDIS_URL)
    await pool.flushdb()
    try:
        yield pool
    finally:
        await pool.flushdb()
        await pool.aclose()


@pytest.fixture
def as_tenant() -> Callable[[UUID], AbstractContextManager[None]]:
    """`with as_tenant(tenant.id): ...` sets the current tenant for the block
    and always resets it afterward, even if the block raises.
    """

    @contextmanager
    def _as_tenant(tenant_id: UUID) -> Iterator[None]:
        token = set_current_tenant(tenant_id)
        try:
            yield
        finally:
            reset_current_tenant(token)

    return _as_tenant


@dataclass
class TenantSeed:
    channel: Channel
    user: User
    conversation: Conversation
    knowledge_base: KnowledgeBase
    operator: Operator
    doctor: Doctor
    appointment: Appointment
    lead: Lead
    message: Message


@dataclass
class Seed:
    tenant_a: Tenant
    tenant_b: Tenant
    a: TenantSeed
    b: TenantSeed


@pytest.fixture
async def seed(
    db_session: AsyncSession, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> Seed:
    """Creates two full tenants (A and B), each with one row in every
    tenant-scoped table plus a conversation + message, via the real
    repositories. Isolation tests use this as their starting data.
    """
    tenant_repo = TenantRepository(db_session)
    tenant_a = await tenant_repo.create(name="Clinic A", status="active")
    tenant_b = await tenant_repo.create(name="Clinic B", status="active")

    async def _build(tenant: Tenant) -> TenantSeed:
        with as_tenant(tenant.id):
            # Stored encrypted, like every real channel row (see
            # app.core.encryption, app.models.channel.Channel.credentials) —
            # app.workers.tasks._send_reply decrypts unconditionally, so a
            # plaintext seed value here would make every worker test that
            # sends a reply fail with DecryptionError instead of testing
            # what it's meant to test. Plaintext value stays "token" so
            # existing assertions on the decrypted value don't need to
            # change.
            channel = await ChannelRepository(db_session).create(
                type="instagram", credentials=encrypt("token"), external_id=f"ig-{tenant.id}"
            )
            user = await UserRepository(db_session).create(
                channel_id=channel.id, external_id=f"ext-{tenant.id}"
            )
            conversation = await ConversationRepository(db_session).create(
                user_id=user.id, status="open"
            )
            knowledge_base = await KnowledgeBaseRepository(db_session).create(
                question="What are your hours?",
                answer="9 to 5.",
                embedding=[0.0] * EMBEDDING_DIMENSIONS,
            )
            operator = await OperatorRepository(db_session).create(
                name="Dr. Smith",
                role="doctor",
                username=f"dr.smith-{tenant.id}",
                password_hash=hash_password("seed-password"),
            )
            doctor = await DoctorRepository(db_session).create(
                name="Dr. Smith",
                specialty="Stomatolog",
                working_hours="09:00 - 18:00",
            )
            appointment = await AppointmentRepository(db_session).create(
                user_id=user.id,
                doctor_id=doctor.id,
                doctor_name=doctor.name,
                scheduled_at=datetime.now(UTC),
                status=AppointmentStatus.SCHEDULED,
            )
            lead = await LeadRepository(db_session).create(
                user_id=user.id,
                conversation_id=conversation.id,
                patient_name="Aziza",
                phone="+998 90 000 00 00",
                topic="implant",
            )
            message = await MessageRepository(db_session).create(
                conversation_id=conversation.id,
                sender="patient",
                content="Hello",
                channel="instagram",
            )
        return TenantSeed(
            channel=channel,
            user=user,
            conversation=conversation,
            knowledge_base=knowledge_base,
            operator=operator,
            doctor=doctor,
            appointment=appointment,
            lead=lead,
            message=message,
        )

    seed_a = await _build(tenant_a)
    seed_b = await _build(tenant_b)
    return Seed(tenant_a=tenant_a, tenant_b=tenant_b, a=seed_a, b=seed_b)
