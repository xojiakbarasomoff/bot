import pytest
from pydantic import ValidationError

from app.core.config import Settings

_BASE_KWARGS = {
    "database_url": "postgresql+asyncpg://test:test@localhost/test",
    "redis_url": "redis://localhost:6379/0",
    "webhook_verify_token": "test-verify-token",
    "meta_app_secret": "test-app-secret",
    # A real (test-only) Fernet key, not an arbitrary string — encryption_key
    # is validated at construction (see test_encryption_key_* below), so
    # every other test in this file needs one that actually passes.
    "encryption_key": "Hq3_REB-V0twf7iBgCPCSUZQiG44egxyiZg9kOKRxUg=",
}


def test_defaults_to_gemini_provider() -> None:
    settings = Settings(**_BASE_KWARGS, gemini_api_key="test-gemini-key")
    assert settings.model_provider == "gemini"


def test_gemini_provider_without_gemini_key_raises() -> None:
    # gemini_api_key must be forced to None explicitly, not just omitted:
    # Settings falls back to the real GEMINI_API_KEY env var for any field
    # not given a constructor value, which would silently defeat this test
    # in an environment (like CI, or this test run) that has one set.
    with pytest.raises(ValidationError, match="GEMINI_API_KEY"):
        Settings(**_BASE_KWARGS, model_provider="gemini", gemini_api_key=None)


def test_openai_provider_without_openai_key_raises() -> None:
    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        Settings(
            **_BASE_KWARGS,
            model_provider="openai",
            openai_api_key=None,
            gemini_api_key="test-gemini-key",
        )


def test_openai_provider_with_openai_key_is_valid() -> None:
    settings = Settings(**_BASE_KWARGS, model_provider="openai", openai_api_key="sk-test")
    assert settings.model_provider == "openai"


def test_gemini_provider_with_gemini_key_is_valid() -> None:
    settings = Settings(**_BASE_KWARGS, model_provider="gemini", gemini_api_key="test-gemini-key")
    assert settings.model_provider == "gemini"


# --- encryption_key ---


def test_missing_encryption_key_raises() -> None:
    kwargs = {**_BASE_KWARGS, "gemini_api_key": "test-gemini-key"}
    del kwargs["encryption_key"]
    # Must be forced to None, not just omitted — same env-var-fallback trap
    # as GEMINI_API_KEY/OPENAI_API_KEY above.
    with pytest.raises(ValidationError, match="encryption_key"):
        Settings(**kwargs, encryption_key=None)  # type: ignore[arg-type]


def test_malformed_encryption_key_raises() -> None:
    with pytest.raises(ValidationError, match="ENCRYPTION_KEY must be a valid Fernet key"):
        Settings(
            **{**_BASE_KWARGS, "encryption_key": "not-a-valid-fernet-key"},
            gemini_api_key="test-gemini-key",
        )


def test_valid_encryption_key_is_accepted() -> None:
    settings = Settings(**_BASE_KWARGS, gemini_api_key="test-gemini-key")
    assert settings.encryption_key == _BASE_KWARGS["encryption_key"]
