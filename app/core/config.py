from functools import lru_cache
from typing import Literal, Self

from cryptography.fernet import Fernet
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    database_url: str = Field(alias="DATABASE_URL")
    redis_url: str = Field(alias="REDIS_URL")
    webhook_verify_token: str = Field(alias="WEBHOOK_VERIFY_TOKEN")
    meta_app_secret: str = Field(alias="META_APP_SECRET")
    # Symmetric key for app.core.encryption (channel.credentials at rest —
    # access tokens, not just placeholders, live there). Required, not
    # optional-with-a-plaintext-fallback: a missing or malformed key must
    # fail app startup loudly rather than let a secret get written in
    # plaintext by accident. Generate with
    # `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
    encryption_key: str = Field(alias="ENCRYPTION_KEY")
    # Signs the operator dashboard's session cookie (app.core.session) —
    # separate from encryption_key since it protects a different thing
    # (tamper-evidence on a cookie, not confidentiality of stored data) and
    # rotating one shouldn't force rotating the other. Generate with
    # `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
    session_secret_key: str = Field(alias="SESSION_SECRET_KEY")
    # False by default so login works over plain http://localhost in local
    # dev — a hardcoded Secure flag would make the browser silently refuse
    # to store the cookie there. Set true explicitly in production (which
    # must be behind HTTPS), rather than inferring it from the request's
    # scheme: inference only works if the deployment correctly forwards
    # X-Forwarded-Proto and uvicorn is run with --proxy-headers, and a
    # misconfiguration there would silently downgrade to an insecure cookie.
    # A config flag fails safe — wrong by default (False, i.e. explicit
    # opt-in) rather than wrong by a proxy-setup mistake.
    session_cookie_secure: bool = Field(default=False, alias="SESSION_COOKIE_SECURE")
    # Meta signs every webhook delivery, and app.api.webhook rejects a
    # mismatch with 403. Setting this false downgrades that rejection to a
    # log line: the check still runs and still reports what it saw, but a
    # request that fails it is processed anyway. That is a deliberate hole
    # in the only evidence a webhook actually came from Meta, so it exists
    # to diagnose a secret mismatch against live traffic -- not as a
    # resting state. Defaults to enforcing, so the hole is only ever opened
    # by someone explicitly setting the variable.
    webhook_signature_enforced: bool = Field(default=True, alias="WEBHOOK_SIGNATURE_ENFORCED")

    # TODO(IGB-?): move onto Tenant/per-tenant settings once the admin panel
    # (TZ 4.2) exists, so each clinic can tune its own debounce window
    # instead of every tenant sharing this one value — same pattern as
    # guardrail.EMERGENCY_RESPONSE / answer.NO_MATCH_RESPONSE.
    debounce_window_seconds: int = Field(default=25, alias="DEBOUNCE_WINDOW_SECONDS")

    # Single switch governing both the LLM and embedding backend (see
    # app.rag.llm._select_llm_provider / app.rag.embeddings._select_embedding_provider).
    # Defaults to gemini: the CEO's direction is Gemini, and we may have no
    # OpenAI key at all — defaulting to openai would fail with a 401 the
    # moment anyone ran this without explicitly setting the provider. openai
    # stays fully implemented and selectable in case we switch back.
    # The Instagram access token this deployment's channel should carry. Read
    # here only so first-run provisioning (app.core.provisioning) can seed a
    # channel with a usable credential; the running pipeline reads the token
    # from channel.credentials, never from configuration.
    access_token: str | None = Field(default=None, alias="ACCESS_TOKEN")
    # Set both to have web startup create the tenant/channel this deployment
    # serves; see app.core.provisioning for why that lives in the app. Unset
    # (the default) skips provisioning entirely, and they can be removed once
    # the rows exist.
    provision_tenant_name: str | None = Field(default=None, alias="PROVISION_TENANT_NAME")
    provision_ig_account_id: str | None = Field(default=None, alias="PROVISION_IG_ACCOUNT_ID")

    model_provider: Literal["openai", "gemini"] = Field(default="gemini", alias="MODEL_PROVIDER")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")

    @field_validator("database_url", mode="after")
    @classmethod
    def _normalize_database_driver(cls, value: str) -> str:
        """Rewrite a driverless Postgres URL onto the asyncpg driver.

        app.core.db builds an *async* engine, which needs an explicit
        async driver in the URL — SQLAlchemy maps a bare `postgresql://`
        onto psycopg2, which isn't a dependency, so the app would die at
        startup with NoSuchModuleError. Managed hosts (Railway, Heroku,
        Fly) inject `postgresql://` (or the older `postgres://` alias) and
        offer no way to change it, so accepting their value and fixing the
        scheme here beats hand-assembling the URL from five separate
        credential variables at every deploy — one mistyped part of which
        fails in a way that looks nothing like a typo.

        Anything already carrying a driver (`postgresql+asyncpg://`, or a
        deliberate `postgresql+psycopg://`) is passed through untouched.
        """
        for scheme in ("postgresql://", "postgres://"):
            if value.startswith(scheme):
                return f"postgresql+asyncpg://{value.removeprefix(scheme)}"
        return value

    @model_validator(mode="after")
    def _require_active_provider_key(self) -> Self:
        if self.model_provider == "openai" and self.openai_api_key is None:
            raise ValueError("OPENAI_API_KEY is required when MODEL_PROVIDER=openai")
        if self.model_provider == "gemini" and self.gemini_api_key is None:
            raise ValueError("GEMINI_API_KEY is required when MODEL_PROVIDER=gemini")
        return self

    @model_validator(mode="after")
    def _require_valid_encryption_key(self) -> Self:
        # Fernet() itself validates: url-safe-base64-decodable and exactly 32
        # bytes once decoded. Both failure modes raise ValueError (binascii's
        # decode error is a ValueError subclass) — re-raised with a message
        # that says what to do about it, not just that construction failed.
        try:
            Fernet(self.encryption_key.encode("utf-8"))
        except ValueError as exc:
            raise ValueError(
                "ENCRYPTION_KEY must be a valid Fernet key (32 url-safe "
                "base64-encoded bytes) — generate one with Fernet.generate_key()"
            ) from exc
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
