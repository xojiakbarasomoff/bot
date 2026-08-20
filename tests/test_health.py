from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_privacy_policy_is_served() -> None:
    """Meta will not publish the app without a reachable privacy-policy
    URL, and an unpublished app receives no Instagram webhooks at all --
    so a 404 here silently costs the deployment its inbound messages.
    Asserts the file is actually found and rendered, not just that a route
    exists: the path is resolved relative to the working directory, which
    is the part that breaks when the Dockerfile stops copying docs/.
    """
    response = client.get("/privacy")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Privacy Policy" in response.text
