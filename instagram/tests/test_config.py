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


# --- database_url driver normalization ---


def test_driverless_postgres_url_gets_asyncpg_driver() -> None:
    settings = Settings(
        **{**_BASE_KWARGS, "database_url": "postgresql://u:p@host:5432/db"},
        gemini_api_key="test-gemini-key",
    )
    assert settings.database_url == "postgresql+asyncpg://u:p@host:5432/db"


def test_legacy_postgres_scheme_gets_asyncpg_driver() -> None:
    # Some managed hosts still hand out the older `postgres://` alias.
    settings = Settings(
        **{**_BASE_KWARGS, "database_url": "postgres://u:p@host:5432/db"},
        gemini_api_key="test-gemini-key",
    )
    assert settings.database_url == "postgresql+asyncpg://u:p@host:5432/db"


def test_explicit_driver_is_left_alone() -> None:
    settings = Settings(
        **{**_BASE_KWARGS, "database_url": "postgresql+psycopg://u:p@host:5432/db"},
        gemini_api_key="test-gemini-key",
    )
    assert settings.database_url == "postgresql+psycopg://u:p@host:5432/db"


def test_password_containing_the_scheme_is_not_mangled() -> None:
    # Only the leading scheme is rewritten — a `postgres://` sitting inside
    # the credentials must survive untouched.
    url = "postgresql+asyncpg://u:postgres%3A//p@host:5432/db"
    settings = Settings(
        **{**_BASE_KWARGS, "database_url": url},
        gemini_api_key="test-gemini-key",
    )
    assert settings.database_url == url


def test_clinic_phone_numbers_default_to_none() -> None:
    """Unset is the supported resting state: app.services.answer drops the
    "call these numbers" half of the pricing fallback rather than letting the
    model produce a number of its own.
    """
    settings = Settings(**_BASE_KWARGS, gemini_api_key="test-gemini-key", clinic_phone_numbers=None)
    assert settings.clinic_phone_numbers is None


def test_pasted_clinic_phone_numbers_are_stripped() -> None:
    """Pasting into a hosting dashboard picks up a trailing newline, and this
    value is read back to patients verbatim.
    """
    settings = Settings(
        **_BASE_KWARGS,
        gemini_api_key="test-gemini-key",
        clinic_phone_numbers="  +998 90 123 45 67\n",
    )
    assert settings.clinic_phone_numbers == "+998 90 123 45 67"
