import re
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime
from uuid import UUID

import httpx
import pytest
from arq.connections import ArqRedis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_db_session
from app.core.passwords import hash_password
from app.core.queue import get_arq_pool
from app.main import app
from app.models.operator import Operator
from app.repositories.appointment import AppointmentRepository
from app.repositories.operator import OperatorRepository
from app.services.appointment import create_appointment
from app.services.login_rate_limit import MAX_LOGIN_ATTEMPTS
from tests.conftest import Seed

TEST_SETTINGS = Settings(
    database_url="postgresql+asyncpg://test:test@localhost/test",
    redis_url="redis://localhost:6379/0",
    openai_api_key="sk-test",
    gemini_api_key="test-gemini-key",
    webhook_verify_token="test-verify-token",
    meta_app_secret="test-app-secret",
)

# A fixed future date, arbitrary but deterministic — avoids "today" flakiness.
BASE_DATE = date(2026, 9, 2)

_CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


@pytest.fixture(autouse=True)
def _override_settings() -> Iterator[None]:
    app.dependency_overrides[get_settings] = lambda: TEST_SETTINGS
    yield
    app.dependency_overrides.pop(get_settings, None)


@pytest.fixture
async def client(
    db_session: AsyncSession, redis_pool: ArqRedis
) -> AsyncIterator[httpx.AsyncClient]:
    """Same in-process ASGI pattern as tests/test_webhook.py's client fixture
    — shares this test's event loop (and so its db_session), unlike
    fastapi.testclient.TestClient. Needs the real redis_pool now too: login
    rate-limiting does real INCR/EXPIRE/GET/DELETE against Redis.
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


async def _create_operator(
    db_session: AsyncSession,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    tenant_id: UUID,
    *,
    username: str,
    password: str,
    role: str,
) -> Operator:
    with as_tenant(tenant_id):
        return await OperatorRepository(db_session).create(
            name="Test Staff",
            role=role,
            username=username,
            password_hash=hash_password(password),
        )


async def _login(client: httpx.AsyncClient, username: str, password: str) -> httpx.Response:
    return await client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    )


def _extract_csrf(html: str) -> str:
    match = _CSRF_RE.search(html)
    assert match is not None, "csrf_token field not found in rendered page"
    return match.group(1)


# --- login ---


async def test_login_page_renders(client: httpx.AsyncClient) -> None:
    response = await client.get("/login")
    assert response.status_code == 200
    assert "csrf" not in response.text  # not logged in yet — no session to bind a CSRF token to


async def test_login_wrong_password_shows_error_and_no_cookie(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    await _create_operator(
        db_session,
        as_tenant,
        seed.tenant_a.id,
        username="op-wrongpw",
        password="right",
        role="operator",
    )
    response = await _login(client, "op-wrongpw", "wrong")
    assert response.status_code == 401
    assert "session" not in response.cookies


async def test_login_unknown_username_shows_error(client: httpx.AsyncClient) -> None:
    response = await _login(client, "no-such-user", "whatever")
    assert response.status_code == 401


async def test_login_success_sets_cookie_and_redirects(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    await _create_operator(
        db_session,
        as_tenant,
        seed.tenant_a.id,
        username="op-ok",
        password="correct-horse",
        role="operator",
    )
    response = await _login(client, "op-ok", "correct-horse")
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard/appointments"
    assert "session" in response.cookies


# --- login rate limiting ---


async def test_login_rate_limits_after_repeated_failures(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    await _create_operator(
        db_session,
        as_tenant,
        seed.tenant_a.id,
        username="op-throttled",
        password="right-password",
        role="operator",
    )

    for _ in range(MAX_LOGIN_ATTEMPTS):
        response = await _login(client, "op-throttled", "wrong-password")
        assert response.status_code == 401

    # Locked out now even with the correct password — rate limiting has to
    # block the attempt itself, not just count failures after the fact.
    response = await _login(client, "op-throttled", "right-password")
    assert response.status_code == 429
    assert "session" not in response.cookies


async def test_login_rate_limit_is_per_username(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    await _create_operator(
        db_session,
        as_tenant,
        seed.tenant_a.id,
        username="op-throttled-a",
        password="pw-a",
        role="operator",
    )
    await _create_operator(
        db_session,
        as_tenant,
        seed.tenant_a.id,
        username="op-throttled-b",
        password="pw-b",
        role="operator",
    )

    for _ in range(MAX_LOGIN_ATTEMPTS):
        await _login(client, "op-throttled-a", "wrong")

    # A different account's own attempts are unaffected by op-throttled-a's lockout.
    response = await _login(client, "op-throttled-b", "pw-b")
    assert response.status_code == 303


# --- access control ---


async def test_dashboard_without_session_redirects_to_login(client: httpx.AsyncClient) -> None:
    response = await client.get("/dashboard/appointments", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_dashboard_tampered_cookie_redirects_to_login(client: httpx.AsyncClient) -> None:
    client.cookies.set("session", "not-a-real-signed-cookie")
    response = await client.get("/dashboard/appointments", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_doctor_can_view_but_not_manage(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    await _create_operator(
        db_session, as_tenant, seed.tenant_a.id, username="doc-a", password="pw", role="doctor"
    )
    await _login(client, "doc-a", "pw")

    response = await client.get(f"/dashboard/appointments?date={BASE_DATE.isoformat()}")
    assert response.status_code == 200
    assert "Book an appointment" not in response.text
    assert "csrf_token" not in response.text  # only rendered inside the manage-only forms


async def test_doctor_cannot_create_appointment(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    await _create_operator(
        db_session, as_tenant, seed.tenant_a.id, username="doc-b", password="pw", role="doctor"
    )
    await _login(client, "doc-b", "pw")

    response = await client.post(
        "/dashboard/appointments",
        data={
            "patient_name": "Ali",
            "date": BASE_DATE.isoformat(),
            "time": "09:00",
            "csrf_token": "irrelevant-doctor-is-blocked-before-csrf-matters",
        },
    )
    assert response.status_code == 403


async def test_create_without_csrf_token_is_rejected(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    await _create_operator(
        db_session, as_tenant, seed.tenant_a.id, username="op-csrf", password="pw", role="operator"
    )
    await _login(client, "op-csrf", "pw")

    response = await client.post(
        "/dashboard/appointments",
        data={
            "patient_name": "Ali",
            "date": BASE_DATE.isoformat(),
            "time": "09:00",
            "csrf_token": "wrong-token",
        },
    )
    assert response.status_code == 403


# --- day view + create + cancel, end to end ---


async def test_operator_books_and_cancels_appointment(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    await _create_operator(
        db_session, as_tenant, seed.tenant_a.id, username="op-flow", password="pw", role="operator"
    )
    await _login(client, "op-flow", "pw")

    day_page = await client.get(f"/dashboard/appointments?date={BASE_DATE.isoformat()}")
    csrf_token = _extract_csrf(day_page.text)

    create_response = await client.post(
        "/dashboard/appointments",
        data={
            "patient_name": "Ali Valiyev",
            "date": BASE_DATE.isoformat(),
            "time": "09:00",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert create_response.status_code == 303
    assert (
        create_response.headers["location"]
        == f"/dashboard/appointments?date={BASE_DATE.isoformat()}"
    )

    after_create = await client.get(f"/dashboard/appointments?date={BASE_DATE.isoformat()}")
    assert "09:00" in after_create.text
    assert "Ali Valiyev" in after_create.text

    with as_tenant(seed.tenant_a.id):
        booked = await AppointmentRepository(db_session).get_active_at(
            datetime(2026, 9, 2, 4, 0, tzinfo=UTC)
        )
    assert booked is not None
    assert booked.status == "scheduled"

    cancel_response = await client.post(
        f"/dashboard/appointments/{booked.id}/cancel",
        data={"date": BASE_DATE.isoformat(), "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert cancel_response.status_code == 303

    with as_tenant(seed.tenant_a.id):
        cancelled = await AppointmentRepository(db_session).get(booked.id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"


async def test_create_appointment_rejects_taken_slot(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    with as_tenant(seed.tenant_a.id):
        await create_appointment(
            AppointmentRepository(db_session),
            scheduled_at=datetime(2026, 9, 2, 4, 30, tzinfo=UTC),
            source="bot",
            patient_name="Existing Patient",
        )

    await _create_operator(
        db_session, as_tenant, seed.tenant_a.id, username="op-taken", password="pw", role="operator"
    )
    await _login(client, "op-taken", "pw")

    day_page = await client.get(f"/dashboard/appointments?date={BASE_DATE.isoformat()}")
    csrf_token = _extract_csrf(day_page.text)

    response = await client.post(
        "/dashboard/appointments",
        data={
            "patient_name": "New Patient",
            "date": BASE_DATE.isoformat(),
            "time": "09:30",
            "csrf_token": csrf_token,
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "already booked" in response.text


# --- tenant isolation ---


async def test_operator_cannot_cancel_other_tenants_appointment(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    with as_tenant(seed.tenant_b.id):
        other_tenant_appt = await create_appointment(
            AppointmentRepository(db_session),
            scheduled_at=datetime(2026, 9, 2, 5, 0, tzinfo=UTC),
            source="bot",
            patient_name="Tenant B Patient",
        )

    await _create_operator(
        db_session,
        as_tenant,
        seed.tenant_a.id,
        username="op-isolated",
        password="pw",
        role="operator",
    )
    await _login(client, "op-isolated", "pw")

    day_page = await client.get(f"/dashboard/appointments?date={BASE_DATE.isoformat()}")
    assert "Tenant B Patient" not in day_page.text
    csrf_token = _extract_csrf(day_page.text)

    response = await client.post(
        f"/dashboard/appointments/{other_tenant_appt.id}/cancel",
        data={"date": BASE_DATE.isoformat(), "csrf_token": csrf_token},
    )
    assert response.status_code == 404

    with as_tenant(seed.tenant_b.id):
        still_scheduled = await AppointmentRepository(db_session).get(other_tenant_appt.id)
    assert still_scheduled is not None
    assert still_scheduled.status == "scheduled"
