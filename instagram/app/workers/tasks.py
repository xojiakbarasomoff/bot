"""Background jobs: turn a recorded inbound message into a sent reply.

Platform-neutral. The job arguments carry a channel id rather than anything
Instagram-shaped, the reply goes out through app.services.delivery (which
dispatches on the channel's type), and every step in between — retrieval,
guardrails, the answer prompt, the transcript — is shared business logic.
The Telegram bot's inbound edge enqueues these same two jobs.
"""

import logging
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import AsyncSession

# Imported for the side effect of registering the built-in channel adapters,
# which app.services.delivery then looks up by channel type. The worker is a
# separate process from the web app, so it must do this itself.
from app import channels  # noqa: F401  - importing it registers the adapters
from app.channels.base import ChannelAdapter
from app.core.config import get_settings
from app.core.db import db_session
from app.core.logging import configure_logging
from app.core.redaction import preview
from app.core.tenant_context import reset_current_tenant, set_current_tenant
from app.models.channel import Channel
from app.models.conversation import Conversation
from app.rag.embeddings import EmbeddingProvider
from app.rag.llm import LLMProvider
from app.repositories.appointment import AppointmentRepository
from app.services.answer import generate_answer
from app.services.booking import settle as settle_booking
from app.services.conversation import (
    context_for_reply,
    last_inbound_at,
    record_outbound_message,
)
from app.services.debounce import join_messages, pop_batch_if_current_generation
from app.services.delivery import send_reply
from app.services.reminders import send_due_reminders

logger = logging.getLogger(__name__)


async def process_inbound_message(
    ctx: dict[str, Any],
    tenant_id: str,
    channel_id: str,
    conversation_id: str,
    sender_external_id: str,
    message_text: str,
    reply_context: Mapping[str, Any] | None = None,
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] = db_session,
    embedding_provider: EmbeddingProvider | None = None,
    llm_provider: LLMProvider | None = None,
    adapter: ChannelAdapter | None = None,
) -> None:
    """ARQ job: answer one inbound message (or one debounced batch) and send
    the reply back over the channel it arrived on.

    Runs in the worker process, not the request that received the webhook —
    that request's DB session and tenant context don't survive past its 200
    response, so this re-establishes both from the plain, serializable
    arguments the inbound edge enqueued (a UUID isn't one of those, hence
    the ids arrive as str and are parsed back here).

    The channel id is carried explicitly rather than looked up from the
    tenant, so the reply always goes out over the account the patient
    actually wrote to — see app.services.delivery for what the previous
    "first active channel" lookup got wrong.

    session_factory defaults to app.core.db.db_session (a genuinely fresh
    session against the real engine — in production this job never receives
    one from a caller). It is injectable for the same reason
    embedding_provider/llm_provider/adapter are: tests call this function
    directly, bypassing the queue. That is not just convenience — a *second*,
    independent db_session() in a test cannot see that test's own uncommitted
    fixture data, so tests must be able to point the job at the same
    transactional session the test itself is using.
    """
    tenant_uuid = uuid.UUID(tenant_id)
    conversation_uuid = uuid.UUID(conversation_id)
    token = set_current_tenant(tenant_uuid)
    try:
        async with session_factory() as session:
            history = await context_for_reply(session, conversation_uuid)
            reply = await generate_answer(
                session,
                message_text,
                embedding_provider=embedding_provider,
                llm_provider=llm_provider,
                history=history,
            )

            # The reply may carry a booking the assistant agreed to. Settled
            # here rather than inside generate_answer: that function reads,
            # and this writes a row the clinic will act on, so it belongs in
            # the same place as the rest of this task's transaction.
            conversation = await session.get(Conversation, conversation_uuid)
            channel = await session.get(Channel, uuid.UUID(channel_id))
            if conversation is not None:
                reply, appointment = await settle_booking(
                    AppointmentRepository(session),
                    reply,
                    user_id=conversation.user_id,
                    conversation_id=conversation_uuid,
                    # "instagram" / "telegram", so the dashboard's SOURCE
                    # column says where the booking came from — beside
                    # "operator" for the ones staff enter by hand.
                    source=str(channel.type) if channel is not None else "bot",
                )
                if appointment is not None:
                    await session.commit()

            # Full reply text stays out of INFO — it is patient-adjacent
            # content that should not sit in logs that may ship to external
            # monitoring (TZ section 7, personal data). Length + truncated
            # preview at INFO; full text only at DEBUG, under a distinct
            # event name so it is never ambiguous with the redacted INFO line.
            logger.info(
                "webhook_reply_generated",
                extra={
                    "tenant_id": tenant_id,
                    "conversation_id": conversation_id,
                    "sender_external_id": sender_external_id,
                    "history_turns": len(history),
                    "reply_length": len(reply),
                    "reply_preview": preview(reply),
                },
            )
            logger.debug(
                "webhook_reply_full_text",
                extra={"sender_external_id": sender_external_id, "reply": reply},
            )

            # The real recorded time the patient last wrote, now that inbound
            # messages are persisted — not the "assume now" placeholder this
            # used before, which left the reply-window check unable to
            # observe staleness at all. Falling back to now() only covers a
            # conversation whose inbound row is somehow missing, where
            # refusing to reply would be the worse failure.
            patient_last_wrote = await last_inbound_at(session, conversation_uuid)
            delivered_over = await send_reply(
                session,
                channel_id=uuid.UUID(channel_id),
                recipient_external_id=sender_external_id,
                text=reply,
                last_user_message_at=patient_last_wrote or datetime.now(UTC),
                reply_context=reply_context,
                adapter=adapter,
            )

            # Recorded only when it actually went out: a reply in the
            # transcript the patient never received would make the next
            # reply's history describe a conversation that did not happen.
            if delivered_over is not None:
                await record_outbound_message(
                    session,
                    conversation_id=conversation_uuid,
                    channel_type=delivered_over,
                    text=reply,
                )
                await session.commit()
    finally:
        reset_current_tenant(token)


async def fire_debounce_window(
    ctx: dict[str, Any],
    tenant_id: str,
    channel_id: str,
    conversation_id: str,
    sender_external_id: str,
    generation: int,
    reply_context: Mapping[str, Any] | None = None,
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] = db_session,
    embedding_provider: EmbeddingProvider | None = None,
    llm_provider: LLMProvider | None = None,
    adapter: ChannelAdapter | None = None,
) -> None:
    """ARQ job: fires once a patient's debounce window has elapsed with no
    further messages. Scheduled (deferred) by
    app.services.debounce.handle_inbound_message for every non-emergency
    message; most scheduled calls for a burst of messages from the same
    patient are stale by the time they run (a later message reset the
    window) and no-op here via pop_batch_if_current_generation — only the
    last one scheduled for the current generation actually claims and
    processes the batch.

    Uses ctx["redis"] (the worker's own pool, set by arq itself) rather than
    a second cached pool, and hands the claimed batch to
    process_inbound_message unchanged — same generate-and-send logic,
    whether it is answering one message or a joined batch of several.
    """
    pool = ctx["redis"]
    messages = await pop_batch_if_current_generation(
        pool, uuid.UUID(tenant_id), uuid.UUID(channel_id), sender_external_id, generation
    )
    if not messages:
        return

    await process_inbound_message(
        ctx,
        tenant_id,
        channel_id,
        conversation_id,
        sender_external_id,
        join_messages(messages),
        reply_context,
        session_factory=session_factory,
        embedding_provider=embedding_provider,
        llm_provider=llm_provider,
        adapter=adapter,
    )


async def send_appointment_reminders(
    ctx: dict[str, Any],
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] = db_session,
    now: datetime | None = None,
    adapter: ChannelAdapter | None = None,
) -> None:
    """Cron job: remind patients about appointments that are coming up.

    Runs across every tenant — it has no request and no operator to take one
    from — and app.services.reminders sets the tenant per appointment before
    touching anything scoped. See that module for why a reminder is marked
    sent only once it has actually gone out.
    """
    async with session_factory() as session:
        run = await send_due_reminders(session, now=now, adapter=adapter)

    if run.sent or run.failed or run.skipped:
        logger.info(
            "appointment_reminders_run",
            extra={"sent": run.sent, "failed": run.failed, "skipped": run.skipped},
        )


# The worker is a separate process from the web app, so it needs its own
# handler installed -- app.main's call never runs here.
configure_logging()


class WorkerSettings:
    functions = [process_inbound_message, fire_debounce_window]
    # Every five minutes. The reminder windows are hours wide and the job
    # catches up on anything it missed, so this is about how promptly a
    # reminder lands inside its window rather than about not losing one.
    cron_jobs = [cron(send_appointment_reminders, minute=set(range(0, 60, 5)))]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
