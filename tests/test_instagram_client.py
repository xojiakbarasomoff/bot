import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.channels.instagram.client import (
    GraphAPIInstagramClient,
    InstagramSendError,
    is_placeholder_credential,
    is_within_messaging_window,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


# --- is_within_messaging_window ---


def test_within_window_just_under_24h() -> None:
    last_message_at = NOW - timedelta(hours=23, minutes=59)
    assert is_within_messaging_window(last_message_at, now=NOW) is True


def test_exactly_24h_is_still_within_window() -> None:
    last_message_at = NOW - timedelta(hours=24)
    assert is_within_messaging_window(last_message_at, now=NOW) is True


def test_outside_window_just_over_24h() -> None:
    last_message_at = NOW - timedelta(hours=24, minutes=1)
    assert is_within_messaging_window(last_message_at, now=NOW) is False


def test_outside_window_days_stale() -> None:
    last_message_at = NOW - timedelta(days=3)
    assert is_within_messaging_window(last_message_at, now=NOW) is False


# --- is_placeholder_credential ---


@pytest.mark.parametrize(
    "credentials", ["", "  ", "placeholder", "PLACEHOLDER", "TODO", "changeme", "pending"]
)
def test_placeholder_credentials_are_detected(credentials: str) -> None:
    assert is_placeholder_credential(credentials) is True


@pytest.mark.parametrize("credentials", ["token", "EAAG...realtoken", "PLACEHOLDERX"])
def test_real_looking_credentials_are_not_placeholders(credentials: str) -> None:
    assert is_placeholder_credential(credentials) is False


# --- GraphAPIInstagramClient ---


async def test_send_text_posts_recipient_and_message_with_token_as_query_param() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"recipient_id": "sender-1", "message_id": "mid.1"})

    client = GraphAPIInstagramClient()
    client._http = httpx.AsyncClient(
        base_url=client._http.base_url, transport=httpx.MockTransport(handler)
    )

    await client.send_text(access_token="secret-token", recipient_igsid="sender-1", text="Hi!")

    [request] = captured
    assert request.url.params["access_token"] == "secret-token"
    assert request.url.path.endswith("/me/messages")
    assert json.loads(request.content) == {
        "recipient": {"id": "sender-1"},
        "message": {"text": "Hi!"},
    }


async def test_send_text_raises_on_error_response_without_leaking_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "Invalid token", "code": 190}})

    client = GraphAPIInstagramClient()
    client._http = httpx.AsyncClient(
        base_url=client._http.base_url, transport=httpx.MockTransport(handler)
    )

    with pytest.raises(InstagramSendError):
        await client.send_text(access_token="bad-token", recipient_igsid="sender-1", text="Hi!")
