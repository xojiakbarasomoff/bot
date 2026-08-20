import logging
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any

from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import db_session
from app.core.encryption import decrypt
from app.core.logging import configure_logging
from app.core.redaction import preview
from app.core.tenant_context import reset_current_tenant, set_current_tenant
from app.rag.embeddings import EmbeddingProvider
from app.rag.llm import LLMProvider
from app.repositories.channel import ChannelRepository
from app.services.answer import generate_answer
from app.services.debounce import join_messages, pop_batch_if_current_generation
from app.services.instagram_client import (
    InstagramClient,
    get_instagram_client,
    is_placeholder_credential,
    is_within_messaging_window,
)

logger = logging.getLogger(__name__)


async def _send_reply(
    session: AsyncSession,
    *,
    sender_igsid: str,
    reply: str,
    last_user_message_at: datetime,
    instagram_client: InstagramClient,
) -> None:
    """Delivers `reply` to sender_igsid via the Instagram Send API, or skips
    cleanly (with a clear log line) when sending isn't possible right now.

    Two conditions skip rather than raise, by design: no real token yet (the
    Instagram token is still blocked as of writing — see
    is_placeholder_credential) and being outside Meta's 24-hour messaging
    window. Both are expected, recoverable states, not bugs — the pipeline
    should keep working end-to-end today and start actually sending the
    moment a real token lands, not crash the job in the meantime. A
    DecryptionError (wrong/rotated ENCRYPTION_KEY, corrupted row) is NOT
    treated as one of those two — that's a real misconfiguration, not "no
    token yet", so it's left to propagate and fail the job loudly.
    """
    channels = await ChannelRepository(session).list(type="instagram", is_active=True)
    channel = next(iter(channels), None)
    if channel is None:
        logger.warning("instagram_send_skipped_no_channel", extra={"sender_igsid": sender_igsid})
        return

    # channel.credentials is always ciphertext at rest (app.core.encryption)
    # — even the "pending" placeholder is stored encrypted, so there's no
    # special-cased plaintext path to accidentally leave un-encrypted.
    access_token = decrypt(channel.credentials)

    if is_placeholder_credential(access_token):
        logger.warning(
            "no_token_configured",
            extra={"sender_igsid": sender_igsid, "channel_id": str(channel.id)},
        )
        return

    if not is_within_messaging_window(last_user_message_at):
        # TODO(IGB-?): Meta allows sending outside the 24h window only with a
        # message tag (e.g. HUMAN_AGENT, CONFIRMED_EVENT_UPDATE) or a paid
        # conversation — not implemented yet. For now the reply is dropped;
        # once tags are supported, retry here with the appropriate tag
        # instead of skipping.
        logger.warning(
            "instagram_send_skipped_outside_window",
            extra={
                "sender_igsid": sender_igsid,
                "last_user_message_at": last_user_message_at.isoformat(),
            },
        )
        return

    # Any failure here (network, non-2xx from Meta) is left to propagate —
    # the client already logs status/error code before raising, and letting
    # the job fail lets arq retry, same as an LLM call failing above.
    await instagram_client.send_text(
        access_token=access_token, recipient_igsid=sender_igsid, text=reply
    )
    logger.info(
        "instagram_reply_sent",
        extra={"sender_igsid": sender_igsid, "reply_length": len(reply)},
    )


async def process_inbound_message(
    ctx: dict[str, Any],
    tenant_id: str,
    sender_igsid: str,
    message_text: str,
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] = db_session,
    embedding_provider: EmbeddingProvider | None = None,
    llm_provider: LLMProvider | None = None,
    instagram_client: InstagramClient | None = None,
    last_user_message_at: datetime | None = None,
) -> None:
    """ARQ job: generate a reply to one inbound Instagram message and send
    it back via the Instagram Send API.

    Runs in the worker process, not the webhook request — the request's DB
    session and tenant context don't survive past the 200 response, so this
    re-establishes both from the plain, serializable arguments the webhook
    enqueued (a UUID isn't one of those, hence tenant_id arrives as str and
    gets parsed back here).

    session_factory defaults to app.core.db.db_session (a genuinely fresh
    session against the real engine — "opens its own session", as in
    production this job never receives one from a caller). It's injectable
    for the same reason embedding_provider/llm_provider/instagram_client
    are: tests call this function directly, bypassing the queue, the same
    pattern used for generate_answer/ingest_faqs elsewhere in this codebase.
    This isn't just convenience — a *second*, independent db_session() in a
    test can't see that test's own uncommitted fixture data (Postgres won't
    show one connection another's uncommitted writes), so tests must be able
    to point the job at the same transactional session the test itself is
    using.

    last_user_message_at feeds the 24-hour messaging window check (see
    app.services.instagram_client.is_within_messaging_window) and defaults
    to "now": nothing in this pipeline persists a real inbound-message
    timestamp yet, and this job only ever runs shortly after the user's
    message arrived (immediately for an emergency, or after the debounce
    window for everything else), so "now" is an accurate enough stand-in.
    Tests inject an explicit (e.g. stale) value to exercise the window check
    deterministically rather than depending on wall-clock timing.

    TODO(IGB-?): this "now" default means the window check can never
    actually observe staleness in production today — it's a placeholder,
    not a real captured value, and the real Instagram token being blocked
    means it can't be exercised against Meta for real yet either. Planned
    follow-up once this lands: add a last_inbound_at column to users, have
    the webhook set it on every inbound message, and switch this parameter
    to read that instead of defaulting to datetime.now(UTC).
    """
    tenant_uuid = uuid.UUID(tenant_id)
    token = set_current_tenant(tenant_uuid)
    try:
        async with session_factory() as session:
            reply = await generate_answer(
                session,
                message_text,
                embedding_provider=embedding_provider,
                llm_provider=llm_provider,
            )

            # Full reply text stays out of INFO — it's patient-adjacent
            # content that shouldn't sit in logs that may ship to external
            # monitoring (TZ section 7, personal data). Length + truncated
            # preview at INFO; full text only at DEBUG, under a distinct
            # event name so it's never ambiguous with the redacted INFO line.
            logger.info(
                "webhook_reply_generated",
                extra={
                    "tenant_id": tenant_id,
                    "sender_igsid": sender_igsid,
                    "reply_length": len(reply),
                    "reply_preview": preview(reply),
                },
            )
            logger.debug(
                "webhook_reply_full_text",
                extra={"sender_igsid": sender_igsid, "reply": reply},
            )

            await _send_reply(
                session,
                sender_igsid=sender_igsid,
                reply=reply,
                last_user_message_at=last_user_message_at or datetime.now(UTC),
                instagram_client=instagram_client or get_instagram_client(),
            )
    finally:
        reset_current_tenant(token)


async def fire_debounce_window(
    ctx: dict[str, Any],
    tenant_id: str,
    sender_igsid: str,
    generation: int,
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] = db_session,
    embedding_provider: EmbeddingProvider | None = None,
    llm_provider: LLMProvider | None = None,
    instagram_client: InstagramClient | None = None,
    last_user_message_at: datetime | None = None,
) -> None:
    """ARQ job: fires once a user's debounce window has elapsed with no
    further messages. Scheduled (deferred) by
    app.services.debounce.handle_inbound_message for every non-emergency
    message; most scheduled calls for a burst of messages from the same
    user are stale by the time they run (a later message reset the window)
    and no-op here via pop_batch_if_current_generation — only the last one
    scheduled for the current generation actually claims and processes the
    batch.

    Uses ctx["redis"] (the worker's own pool, set by arq itself) rather than
    a second cached pool, and hands the claimed batch to
    process_inbound_message unchanged — same generate-and-send logic,
    whether it's answering one message or a joined batch of several.
    """
    pool = ctx["redis"]
    messages = await pop_batch_if_current_generation(
        pool, uuid.UUID(tenant_id), sender_igsid, generation
    )
    if not messages:
        return

    await process_inbound_message(
        ctx,
        tenant_id,
        sender_igsid,
        join_messages(messages),
        session_factory=session_factory,
        embedding_provider=embedding_provider,
        llm_provider=llm_provider,
        instagram_client=instagram_client,
        last_user_message_at=last_user_message_at,
    )


# The worker is a separate process from the web app, so it needs its own
# handler installed -- app.main's call never runs here.
configure_logging()


class WorkerSettings:
    functions = [process_inbound_message, fire_debounce_window]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
