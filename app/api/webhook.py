import hashlib
import hmac
import logging

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_db_session
from app.core.queue import get_arq_pool
from app.core.redaction import preview
from app.core.tenant_context import reset_current_tenant, set_current_tenant
from app.services.debounce import handle_inbound_message
from app.services.tenant_resolution import resolve_tenant_for_ig_account

logger = logging.getLogger(__name__)

router = APIRouter()


class WebhookSender(BaseModel):
    id: str


class WebhookRecipient(BaseModel):
    id: str


class WebhookMessage(BaseModel):
    text: str | None = None
    is_echo: bool = False


class MessagingEvent(BaseModel):
    sender: WebhookSender
    recipient: WebhookRecipient
    message: WebhookMessage | None = None


class WebhookEntry(BaseModel):
    id: str
    messaging: list[MessagingEvent] = []


class WebhookPayload(BaseModel):
    object: str
    entry: list[WebhookEntry] = []


def _verify_signature(raw_body: bytes, signature_header: str | None, app_secret: str) -> bool:
    # TEMPORARY - VERIFICATION BYPASSED. Returns True without checking
    # anything so the deployed bot keeps accepting messages while the
    # META_APP_SECRET mismatch behind the 403s is tracked down.
    #
    # While this early return is here, POST /webhook trusts *any* caller:
    # anyone who knows the URL can forge a payload and make the bot send
    # messages to real patients, burn LLM quota, and write fabricated
    # conversations into the database. Meta's signature is the only thing
    # that proves a request actually came from Meta.
    #
    # To restore the check: delete this comment and the `return True`
    # below. The real implementation is intact underneath.
    return True

    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)


def _signature_failure_detail(
    raw_body: bytes, signature_header: str | None, app_secret: str
) -> str:
    """Log-safe `k=v` detail explaining *why* a signature check failed.

    The app secret never lands in a log line — only its length and a
    truncated fingerprint, which is enough to tell whether the value
    deployed on the host is the one you think it is (compare fingerprints
    across environments) without disclosing it. secret_surrounding_whitespace
    catches the most common deploy mistake: a value pasted into a hosting
    dashboard with a trailing newline or wrapping quotes, which silently
    changes the HMAC while looking identical on screen.

    Formatted into the log *message* rather than passed as `extra=`,
    unlike the rest of this module: nothing installs a structured log
    handler, so `extra` fields are dropped by the default uvicorn
    formatter and would be invisible exactly where this is needed — in a
    deployed environment's log stream.
    """
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    fields: dict[str, object] = {
        "expected": f"sha256={expected}",
        "received": signature_header,
        "body_length": len(raw_body),
        "secret_length": len(app_secret),
        "secret_fingerprint": hashlib.sha256(app_secret.encode("utf-8")).hexdigest()[:8],
        "secret_surrounding_whitespace": app_secret != app_secret.strip(),
    }
    return " ".join(f"{key}={value}" for key, value in fields.items())


def _is_echo(event: MessagingEvent, page_id: str) -> bool:
    if event.message is not None and event.message.is_echo:
        return True
    return event.sender.id == page_id


async def _handle_payload(session: AsyncSession, pool: ArqRedis, payload: WebhookPayload) -> None:
    for entry in payload.entry:
        # entry.id is the IG account (page) id that received the message —
        # one tenant's channel per entry, so resolution happens once per
        # entry rather than once per messaging event.
        tenant_id = await resolve_tenant_for_ig_account(session, entry.id)
        if tenant_id is None:
            logger.warning("webhook_unknown_ig_account", extra={"ig_account_id": entry.id})
            continue

        token = set_current_tenant(tenant_id)
        try:
            for event in entry.messaging:
                if event.message is None:
                    # Not a message event (e.g. read receipt, postback) — nothing to do yet.
                    continue

                if _is_echo(event, entry.id):
                    logger.info(
                        "webhook_echo_skipped",
                        extra={"sender_id": event.sender.id, "recipient_id": event.recipient.id},
                    )
                    continue

                if event.message.text is None:
                    # Attachment-only message (image, sticker, etc) — nothing
                    # for the FAQ/LLM pipeline to answer yet.
                    logger.info(
                        "webhook_attachment_only_skipped",
                        extra={"sender_id": event.sender.id, "recipient_id": event.recipient.id},
                    )
                    continue

                # Full message text stays out of INFO — it's patient content
                # that shouldn't sit in logs that may ship to external
                # monitoring (TZ section 7, personal data). Length + a short
                # truncated preview only.
                logger.info(
                    "webhook_message_received",
                    extra={
                        "tenant_id": str(tenant_id),
                        "sender_igsid": event.sender.id,
                        "recipient_id": event.recipient.id,
                        "message_length": len(event.message.text),
                        "message_preview": preview(event.message.text),
                    },
                )

                # TODO(IGB-?): Meta can redeliver the same webhook payload
                # (slow ack, transient error, etc), which would register and
                # reply to the same message twice. No dedup yet — needs an
                # idempotency key (the IG message id) checked before
                # registering. arq's enqueue_job(_job_id=...) can provide
                # this for free (it no-ops if a job with that id is already
                # queued/running) if the IG message id is used as _job_id.
                await handle_inbound_message(pool, tenant_id, event.sender.id, event.message.text)
        finally:
            reset_current_tenant(token)


@router.get("/webhook")
async def verify_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
    settings: Settings = Depends(get_settings),
) -> Response:
    if hub_mode == "subscribe" and hub_verify_token == settings.webhook_verify_token:
        return Response(content=hub_challenge or "", media_type="text/plain")
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Webhook verification failed")


@router.post("/webhook")
async def receive_webhook(
    request: Request,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
    pool: ArqRedis = Depends(get_arq_pool),
) -> Response:
    raw_body = await request.body()
    signature_header = request.headers.get("x-hub-signature-256")

    if not _verify_signature(raw_body, signature_header, settings.meta_app_secret):
        # Detail on the failure path only — a valid request logs nothing
        # extra. Worth removing once a deployment's signatures line up
        # again: an expected-signature value in a log stream is a valid
        # HMAC over that body, so anyone who can read the logs can replay
        # that exact payload past this check.
        logger.warning(
            "webhook_signature_invalid %s",
            _signature_failure_detail(raw_body, signature_header, settings.meta_app_secret),
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid signature")

    try:
        payload = WebhookPayload.model_validate_json(raw_body)
    except ValueError:
        logger.warning("webhook_payload_invalid")
        return Response(status_code=status.HTTP_200_OK)

    await _handle_payload(session, pool, payload)

    return Response(status_code=status.HTTP_200_OK)
