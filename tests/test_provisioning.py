from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.encryption import decrypt, encrypt
from app.core.provisioning import _provision
from app.models.channel import Channel
from app.models.tenant import Tenant

IG_ACCOUNT_ID = "37823824730565264"


def _settings(**overrides: object) -> Settings:
    base = {
        "database_url": "postgresql+asyncpg://test:test@localhost/test",
        "redis_url": "redis://localhost:6379/0",
        "webhook_verify_token": "test-verify-token",
        "meta_app_secret": "test-app-secret",
        "encryption_key": "Hq3_REB-V0twf7iBgCPCSUZQiG44egxyiZg9kOKRxUg=",
        "gemini_api_key": "test-gemini-key",
    }
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


async def _channel(session: AsyncSession) -> Channel | None:
    result = await session.execute(
        select(Channel).where(Channel.type == "instagram", Channel.external_id == IG_ACCOUNT_ID)
    )
    return result.scalar_one_or_none()


async def test_creates_tenant_and_channel_with_token(db_session: AsyncSession) -> None:
    await _provision(
        db_session,
        ig_account_id=IG_ACCOUNT_ID,
        tenant_name="Klinika",
        access_token="IGAA-real-token",
    )

    channel = await _channel(db_session)
    assert channel is not None
    assert channel.is_active is True
    # Stored encrypted, not in plaintext: the whole point of channel
    # credentials living behind app.core.encryption.
    assert channel.credentials != "IGAA-real-token"
    assert decrypt(channel.credentials) == "IGAA-real-token"

    tenant = await db_session.get(Tenant, channel.tenant_id)
    assert tenant is not None
    assert tenant.name == "Klinika"


async def test_without_token_seeds_a_placeholder(db_session: AsyncSession) -> None:
    """A channel seeded before the token is known must be distinguishable
    from one carrying a real credential -- app.services.instagram_client
    skips sending on a placeholder instead of calling Meta with garbage.
    """
    await _provision(
        db_session, ig_account_id=IG_ACCOUNT_ID, tenant_name="Klinika", access_token=None
    )

    channel = await _channel(db_session)
    assert channel is not None
    assert decrypt(channel.credentials) == "pending"


async def test_rerun_does_not_create_a_second_tenant(db_session: AsyncSession) -> None:
    """Provisioning runs on every web boot, so a redeploy must not keep
    stacking up duplicate tenants for the same Instagram account.
    """
    for _ in range(3):
        await _provision(
            db_session,
            ig_account_id=IG_ACCOUNT_ID,
            tenant_name="Klinika",
            access_token="IGAA-real-token",
        )

    tenants = (await db_session.execute(select(Tenant))).scalars().all()
    channels = (await db_session.execute(select(Channel))).scalars().all()
    assert len(tenants) == 1
    assert len(channels) == 1


async def test_placeholder_credential_is_upgraded_to_a_real_token(
    db_session: AsyncSession,
) -> None:
    """The recovery path that matters: a channel seeded without a token and
    left alone would never be able to reply, and would never say so.
    """
    await _provision(
        db_session, ig_account_id=IG_ACCOUNT_ID, tenant_name="Klinika", access_token=None
    )
    await _provision(
        db_session,
        ig_account_id=IG_ACCOUNT_ID,
        tenant_name="Klinika",
        access_token="IGAA-real-token",
    )

    channel = await _channel(db_session)
    assert channel is not None
    assert decrypt(channel.credentials) == "IGAA-real-token"


async def test_existing_real_token_is_not_overwritten(db_session: AsyncSession) -> None:
    """Provisioning must never clobber a credential someone set deliberately
    (scripts/set_channel_credentials.py, or a token rotated by hand) with a
    stale value still sitting in the deployment's environment.
    """
    tenant = Tenant(name="Klinika", status="active")
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        Channel(
            tenant_id=tenant.id,
            type="instagram",
            external_id=IG_ACCOUNT_ID,
            credentials=encrypt("IGAA-token-set-by-hand"),
            is_active=True,
        )
    )
    await db_session.commit()

    await _provision(
        db_session,
        ig_account_id=IG_ACCOUNT_ID,
        tenant_name="Different Name",
        access_token="IGAA-stale-env-token",
    )

    channel = await _channel(db_session)
    assert channel is not None
    assert decrypt(channel.credentials) == "IGAA-token-set-by-hand"


def test_unconfigured_settings_leave_provisioning_disarmed() -> None:
    # The default must be "do nothing": an unset variable is not an
    # instruction to invent a tenant.
    settings = _settings()
    assert settings.provision_ig_account_id is None
    assert settings.provision_tenant_name is None
