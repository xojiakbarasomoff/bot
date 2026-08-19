import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.channel import Channel


async def resolve_tenant_for_ig_account(
    session: AsyncSession, ig_account_id: str
) -> uuid.UUID | None:
    """Look up which tenant owns the Instagram channel with this account
    (page) id, or None if no active channel matches — an unknown account
    must not be processed.

    This runs BEFORE any tenant is known: it's what establishes tenant
    context for a webhook entry, so unlike the rest of the service layer it
    queries Channel directly rather than through ChannelRepository /
    TenantScopedRepository, both of which require get_current_tenant() to
    already have a value.
    """
    stmt = select(Channel.tenant_id).where(
        Channel.type == "instagram",
        Channel.external_id == ig_account_id,
        Channel.is_active.is_(True),
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
