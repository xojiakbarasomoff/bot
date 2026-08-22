"""First-run provisioning of the tenant and Instagram channel a deployment serves.

scripts/bootstrap_tenant.py does the same thing from an operator's shell, and
stays the right tool wherever one is available. This exists because on a
managed host the database is reachable only from inside the cluster's private
network: the sole process that can write these rows is the application itself,
so provisioning has to be something the app can do on its own behalf, driven
by configuration.

Runs on web startup only (app.main's lifespan), never in the worker, so two
processes booting from the same image cannot race to insert the same channel.
Idempotent regardless: an existing channel is left alone apart from having a
placeholder credential filled in.

Set PROVISION_TENANT_NAME and PROVISION_IG_ACCOUNT_ID to arm it; leave either
unset and startup skips this entirely. Once the rows exist the variables can
be removed -- they are an instruction to provision, not a description of the
running system.
"""

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import ChannelType
from app.channels.instagram.client import is_placeholder_credential
from app.core.config import Settings
from app.core.db import db_session
from app.core.encryption import decrypt, encrypt
from app.models.channel import Channel
from app.models.tenant import Tenant

logger = logging.getLogger(__name__)

CHANNEL_TYPE = ChannelType.INSTAGRAM

# What a channel's credentials hold until a real token is supplied.
# app.channels.instagram.client reads it as "not configured yet" and skips
# sending, rather than calling Meta with a value that cannot work.
_PLACEHOLDER = "pending"

# Long enough for a healthy database on the same private network, short
# enough that an unhealthy one does not keep the web process from serving.
_STARTUP_TIMEOUT_SECONDS = 15.0


def _credential_plaintext(channel: Channel) -> str:
    """The channel's credential in plaintext, or the placeholder when it
    cannot be read. A credential encrypted under a rotated-away key is not a
    reason to fail startup -- it is a reason to treat the channel as
    unconfigured, which is exactly what the placeholder means.
    """
    try:
        return decrypt(channel.credentials)
    except Exception:  # noqa: BLE001 - any decrypt failure means "unreadable"
        return _PLACEHOLDER


async def _provision(
    session: AsyncSession,
    *,
    ig_account_id: str,
    tenant_name: str,
    access_token: str | None,
) -> None:
    result = await session.execute(
        select(Channel).where(Channel.type == CHANNEL_TYPE, Channel.external_id == ig_account_id)
    )
    channel = result.scalar_one_or_none()

    if channel is None:
        tenant = Tenant(name=tenant_name, status="active")
        session.add(tenant)
        await session.flush()
        session.add(
            Channel(
                tenant_id=tenant.id,
                type=CHANNEL_TYPE,
                external_id=ig_account_id,
                credentials=encrypt(access_token or _PLACEHOLDER),
                is_active=True,
            )
        )
        await session.commit()
        # WARNING, not INFO: "this deployment created its tenant" is a line
        # worth finding later without having to widen a log filter.
        logger.warning(
            "provisioned_channel tenant_id=%s ig_account_id=%s has_token=%s",
            tenant.id,
            ig_account_id,
            bool(access_token),
        )
        return

    # Already provisioned. The one thing still worth doing is replacing a
    # placeholder credential with a real token: a channel seeded before the
    # token existed would otherwise stay permanently unable to reply, and
    # silently, since the client skips placeholder credentials rather than
    # raising.
    if access_token and is_placeholder_credential(_credential_plaintext(channel)):
        channel.credentials = encrypt(access_token)
        await session.commit()
        logger.warning(
            "provisioned_channel_token_filled channel_id=%s ig_account_id=%s",
            channel.id,
            ig_account_id,
        )
        return

    logger.warning(
        "provisioning_noop_channel_exists channel_id=%s ig_account_id=%s",
        channel.id,
        ig_account_id,
    )


async def provision_channel_if_configured(settings: Settings) -> None:
    """Create the configured tenant/channel when it is missing.

    No-op when unconfigured, and never fatal: a provisioning failure must not
    stop the web process from serving the webhook, since continuing to accept
    Meta's deliveries is worth more than refusing to start over a seeding
    problem someone can fix afterwards.
    """
    ig_account_id = settings.provision_ig_account_id
    tenant_name = settings.provision_tenant_name
    if ig_account_id is None or tenant_name is None:
        return

    async def _run() -> None:
        async with db_session() as session:
            await _provision(
                session,
                ig_account_id=ig_account_id,
                tenant_name=tenant_name,
                access_token=settings.access_token,
            )

    try:
        # Bounded, because this sits in the startup path: an unreachable
        # database would otherwise hold the port closed for asyncpg's own
        # connect timeout, failing the platform's health check and rolling
        # the deploy back -- the opposite of what "never fatal" is for.
        await asyncio.wait_for(_run(), timeout=_STARTUP_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.error(
            "provisioning_timed_out ig_account_id=%s seconds=%s",
            ig_account_id,
            _STARTUP_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.exception("provisioning_failed ig_account_id=%s", ig_account_id)
