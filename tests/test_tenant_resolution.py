from sqlalchemy.ext.asyncio import AsyncSession

from app.services.tenant_resolution import resolve_tenant_for_ig_account
from tests.conftest import Seed


async def test_resolves_known_ig_account_to_its_tenant(
    db_session: AsyncSession, seed: Seed
) -> None:
    tenant_id = await resolve_tenant_for_ig_account(db_session, seed.a.channel.external_id)
    assert tenant_id == seed.tenant_a.id


async def test_unknown_ig_account_returns_none(db_session: AsyncSession, seed: Seed) -> None:
    tenant_id = await resolve_tenant_for_ig_account(db_session, "no-such-account")
    assert tenant_id is None


async def test_two_tenants_with_different_ig_accounts_do_not_cross_resolve(
    db_session: AsyncSession, seed: Seed
) -> None:
    resolved_a = await resolve_tenant_for_ig_account(db_session, seed.a.channel.external_id)
    resolved_b = await resolve_tenant_for_ig_account(db_session, seed.b.channel.external_id)

    assert resolved_a == seed.tenant_a.id
    assert resolved_b == seed.tenant_b.id
    assert resolved_a != resolved_b
