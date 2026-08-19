from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.core.tenant_context import get_current_tenant
from app.models.appointment import Appointment
from app.repositories.base import CrossTenantAccessError, TenantScopedRepository


class AppointmentRepository(TenantScopedRepository[Appointment]):
    model = Appointment

    async def get_active_at(self, scheduled_at: datetime) -> Appointment | None:
        """The currently-booked (status='scheduled') appointment at this
        exact instant, if any — mirrors what the partial unique index
        enforces at the DB level.
        """
        return await self._get(
            tenant_id=get_current_tenant(), scheduled_at=scheduled_at, status="scheduled"
        )

    async def list_active_between(self, start: datetime, end: datetime) -> Sequence[Appointment]:
        """Every active booking in [start, end) for the current tenant.

        Used by find_next_free_slot() to fetch a whole search window's busy
        slots in one round trip, rather than one query per candidate slot.
        """
        stmt = select(Appointment).where(
            Appointment.tenant_id == get_current_tenant(),
            Appointment.status == "scheduled",
            Appointment.scheduled_at >= start,
            Appointment.scheduled_at < end,
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def list_between(self, start: datetime, end: datetime) -> Sequence[Appointment]:
        """Every booking in [start, end) regardless of status, sorted by
        scheduled_at — the dashboard's day view, which (unlike
        list_active_between) needs to show cancelled bookings too, not just
        active ones.
        """
        stmt = (
            select(Appointment)
            .where(
                Appointment.tenant_id == get_current_tenant(),
                Appointment.scheduled_at >= start,
                Appointment.scheduled_at < end,
            )
            .order_by(Appointment.scheduled_at)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def update(self, obj: Appointment, **values: Any) -> Appointment:
        """Mutates and flushes an already-loaded row (e.g. cancelling).

        Re-checks obj.tenant_id against the current tenant itself — same
        defensive pattern as KnowledgeBaseRepository.update() — so a future
        caller that skips a tenant-scoped fetch fails loudly instead of
        silently writing across tenants.
        """
        current_tenant_id = get_current_tenant()
        if obj.tenant_id != current_tenant_id:
            raise CrossTenantAccessError(
                f"obj.tenant_id={obj.tenant_id} does not match the current tenant "
                f"({current_tenant_id})"
            )
        for field, value in values.items():
            setattr(obj, field, value)
        await self.session.flush()
        return obj
