import hashlib
import hmac
import json
import logging
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from arq.connections import ArqRedis
from arq.jobs import Job
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_db_session
from app.core.queue import get_arq_pool
from app.main import app
from app.services.debounce import FIRE_DEBOUNCE_WINDOW_JOB
from tests.conftest import Seed

TEST_SETTINGS = Settings(
    database_url="postgresql+asyncpg://test:test@localhost/test",
    redis_url="redis://localhost:6379/0",
    openai_api_key="sk-test",
    gemini_api_key="test-gemini-key",
    webhook_verify_token="test-verify-token",
    meta_app_secret="test-app-secret",
)


@pytest.fixture(autouse=True)
def _override_settings() -> Iterator[None]:
    app.dependency_overrides[get_settings] = lambda: TEST_SETTINGS
    yield
    app.dependency_overrides.pop(get_settings, None)


@pytest.fixture
async def client(
    db_session: AsyncSession, redis_pool: ArqRedis
) -> AsyncIterator[httpx.AsyncClient]:
    """An httpx.AsyncClient driving the app in-process over ASGI, sharing
    this test's event loop (and so its db_session) — unlike
    fastapi.testclient.TestClient, which runs the app on a separate thread
    with its own event loop and would break the asyncpg connection bound to
    db_session's loop.

    Uses the real redis_pool fixture (dedicated test Redis DB) rather than a
    mock: handle_inbound_message does real RPUSH/LRANGE/Lua-script work that
    isn't meaningfully fakeable.
    """

    async def _get_db_session_override() -> AsyncSession:
        return db_session

    async def _get_arq_pool_override() -> ArqRedis:
        return redis_pool

    app.dependency_overrides[get_db_session] = _get_db_session_override
    app.dependency_overrides[get_arq_pool] = _get_arq_pool_override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    app.dependency_overrides.pop(get_db_session, None)
    app.dependency_overrides.pop(get_arq_pool, None)


async def _queued_jobs(pool: ArqRedis) -> list[Job]:
    job_ids = await pool.zrange("arq:queue", 0, -1)
    return [Job(job_id.decode(), redis=pool, _queue_name="arq:queue") for job_id in job_ids]


def _sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# --- GET /webhook verification handshake ---


async def test_verify_webhook_with_valid_token_returns_challenge(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "test-verify-token",
            "hub.challenge": "12345",
        },
    )
    assert response.status_code == 200
    assert response.text == "12345"


async def test_verify_webhook_with_invalid_token_returns_403(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong-token",
            "hub.challenge": "12345",
        },
    )
    assert response.status_code == 403


async def test_verify_webhook_with_wrong_mode_returns_403(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/webhook",
        params={
            "hub.mode": "unsubscribe",
            "hub.verify_token": "test-verify-token",
            "hub.challenge": "12345",
        },
    )
    assert response.status_code == 403


# --- POST /webhook signature validation ---


async def test_receive_webhook_with_valid_signature_returns_200(client: httpx.AsyncClient) -> None:
    body = json.dumps({"object": "instagram", "entry": []}).encode("utf-8")
    signature = _sign(body, "test-app-secret")

    response = await client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": signature}
    )
    assert response.status_code == 200


async def test_receive_webhook_with_invalid_signature_returns_403(
    client: httpx.AsyncClient,
) -> None:
    body = json.dumps({"object": "instagram", "entry": []}).encode("utf-8")

    response = await client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": "sha256=" + "0" * 64}
    )
    assert response.status_code == 403


async def test_receive_webhook_with_missing_signature_returns_403(
    client: httpx.AsyncClient,
) -> None:
    body = json.dumps({"object": "instagram", "entry": []}).encode("utf-8")

    response = await client.post("/webhook", content=body)
    assert response.status_code == 403


async def test_unenforced_invalid_signature_is_processed_not_rejected(
    client: httpx.AsyncClient,
) -> None:
    """WEBHOOK_SIGNATURE_ENFORCED=false is a diagnostic mode: the check
    still runs and still reports, but a request that fails it is handled
    instead of refused. Asserted explicitly because the whole point of the
    flag is to change the outcome of a failing check, and a regression
    that silently kept rejecting would look identical from the outside to
    the flag not being read at all.
    """
    unenforced = TEST_SETTINGS.model_copy(update={"webhook_signature_enforced": False})
    app.dependency_overrides[get_settings] = lambda: unenforced
    try:
        body = json.dumps({"object": "instagram", "entry": []}).encode("utf-8")
        response = await client.post(
            "/webhook", content=body, headers={"X-Hub-Signature-256": "sha256=" + "0" * 64}
        )
    finally:
        app.dependency_overrides[get_settings] = lambda: TEST_SETTINGS
    assert response.status_code == 200


def test_signature_enforcement_defaults_to_on() -> None:
    # The insecure mode must never be what you get by forgetting to set
    # the variable.
    assert TEST_SETTINGS.webhook_signature_enforced is True


async def test_receive_webhook_succeeds_with_no_session_cookie_and_no_csrf_token(
    client: httpx.AsyncClient,
) -> None:
    """The dashboard's session/CSRF layer (app.api.auth) must never reach
    the webhook — Meta calls this endpoint with no session cookie and no
    CSRF token, authenticating solely via X-Hub-Signature-256. Nothing here
    sends a cookie or a csrf_token field; a signed request must still
    succeed exactly as it did before that layer existed, proving isolation
    isn't just "no test caught a regression" but an explicit, permanent
    guarantee.
    """
    assert "session" not in client.cookies
    body = json.dumps({"object": "instagram", "entry": []}).encode("utf-8")
    signature = _sign(body, "test-app-secret")

    response = await client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": signature}
    )

    assert response.status_code == 200
    assert "session" not in response.cookies


# --- tenant resolution + echo filtering + debounce registration ---


def _messaging_payload(
    page_id: str,
    sender_id: str,
    recipient_id: str,
    text: str | None,
    is_echo: bool = False,
) -> bytes:
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": page_id,
                "messaging": [
                    {
                        "sender": {"id": sender_id},
                        "recipient": {"id": recipient_id},
                        "message": {"text": text, "is_echo": is_echo},
                    }
                ],
            }
        ],
    }
    return json.dumps(payload).encode("utf-8")


async def test_receive_webhook_skips_echo_event(
    client: httpx.AsyncClient,
    seed: Seed,
    redis_pool: ArqRedis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    page_id = seed.a.channel.external_id
    body = _messaging_payload(page_id, page_id, "user-1", "We'll be right with you", is_echo=True)
    signature = _sign(body, "test-app-secret")

    with caplog.at_level(logging.INFO, logger="app.api.webhook"):
        response = await client.post(
            "/webhook", content=body, headers={"X-Hub-Signature-256": signature}
        )

    assert response.status_code == 200
    assert "webhook_echo_skipped" in caplog.text
    assert "webhook_message_received" not in caplog.text
    assert await _queued_jobs(redis_pool) == []


async def test_receive_webhook_skips_event_from_own_account_without_echo_flag(
    client: httpx.AsyncClient,
    seed: Seed,
    redis_pool: ArqRedis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    page_id = seed.a.channel.external_id
    body = _messaging_payload(page_id, page_id, "user-1", "Auto message", is_echo=False)
    signature = _sign(body, "test-app-secret")

    with caplog.at_level(logging.INFO, logger="app.api.webhook"):
        response = await client.post(
            "/webhook", content=body, headers={"X-Hub-Signature-256": signature}
        )

    assert response.status_code == 200
    assert "webhook_echo_skipped" in caplog.text
    assert await _queued_jobs(redis_pool) == []


async def test_receive_webhook_processes_genuine_inbound_message(
    client: httpx.AsyncClient,
    seed: Seed,
    redis_pool: ArqRedis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    page_id = seed.a.channel.external_id
    text = "Hi, do you have an opening tomorrow?"
    body = _messaging_payload(page_id, "user-1", page_id, text)
    signature = _sign(body, "test-app-secret")

    with caplog.at_level(logging.INFO, logger="app.api.webhook"):
        response = await client.post(
            "/webhook", content=body, headers={"X-Hub-Signature-256": signature}
        )

    assert response.status_code == 200
    assert "webhook_echo_skipped" not in caplog.text

    # Full patient message text must not appear in the INFO record — only
    # length + a short preview (TZ section 7, personal data).
    [record] = [r for r in caplog.records if r.message == "webhook_message_received"]
    assert record.tenant_id == str(seed.tenant_a.id)  # type: ignore[attr-defined]
    assert record.message_length == len(text)  # type: ignore[attr-defined]
    assert record.message_preview == text  # type: ignore[attr-defined]
    assert "message_text" not in record.__dict__

    # The webhook returns 200 without generating the reply inline (no
    # OpenAI call in the request path) — it hands off to the debounce layer,
    # which schedules a deferred fire_debounce_window job rather than
    # calling process_inbound_message directly.
    [job] = await _queued_jobs(redis_pool)
    info = await job.info()
    assert info is not None
    assert info.function == FIRE_DEBOUNCE_WINDOW_JOB
    assert info.args == (str(seed.tenant_a.id), "user-1", 1)


async def test_receive_webhook_from_unknown_ig_account_is_skipped_and_logged(
    client: httpx.AsyncClient,
    redis_pool: ArqRedis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = _messaging_payload(
        "no-such-page", "user-1", "no-such-page", "Hi, do you have an opening tomorrow?"
    )
    signature = _sign(body, "test-app-secret")

    with caplog.at_level(logging.INFO, logger="app.api.webhook"):
        response = await client.post(
            "/webhook", content=body, headers={"X-Hub-Signature-256": signature}
        )

    assert response.status_code == 200
    assert "webhook_unknown_ig_account" in caplog.text
    assert "webhook_message_received" not in caplog.text
    assert await _queued_jobs(redis_pool) == []


async def test_receive_webhook_tenant_b_account_resolves_to_tenant_b_not_a(
    client: httpx.AsyncClient,
    seed: Seed,
    redis_pool: ArqRedis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    page_id = seed.b.channel.external_id
    text = "What are your hours?"
    body = _messaging_payload(page_id, "user-1", page_id, text)
    signature = _sign(body, "test-app-secret")

    with caplog.at_level(logging.INFO, logger="app.api.webhook"):
        response = await client.post(
            "/webhook", content=body, headers={"X-Hub-Signature-256": signature}
        )

    assert response.status_code == 200
    [record] = [r for r in caplog.records if r.message == "webhook_message_received"]
    assert record.tenant_id == str(seed.tenant_b.id)  # type: ignore[attr-defined]
    assert record.tenant_id != str(seed.tenant_a.id)  # type: ignore[attr-defined]

    [job] = await _queued_jobs(redis_pool)
    info = await job.info()
    assert info is not None
    assert info.args == (str(seed.tenant_b.id), "user-1", 1)


async def test_receive_webhook_attachment_only_message_is_skipped_and_not_enqueued(
    client: httpx.AsyncClient,
    seed: Seed,
    redis_pool: ArqRedis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    page_id = seed.a.channel.external_id
    body = _messaging_payload(page_id, "user-1", page_id, None)
    signature = _sign(body, "test-app-secret")

    with caplog.at_level(logging.INFO, logger="app.api.webhook"):
        response = await client.post(
            "/webhook", content=body, headers={"X-Hub-Signature-256": signature}
        )

    assert response.status_code == 200
    assert "webhook_attachment_only_skipped" in caplog.text
    assert "webhook_message_received" not in caplog.text
    assert await _queued_jobs(redis_pool) == []
