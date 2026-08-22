"""Appointments and the numbers the dashboard opens on."""

import uuid
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.api.admin.deps import require_manage_role, verify_csrf_header
from app.api.admin.schemas import (
    AnalyticsSummary,
    AppointmentCreate,
    AppointmentOut,
    DayCount,
)
from app.api.auth import get_current_operator
from app.core.db import get_db_session
from app.core.tenant_context import get_current_tenant
from app.models.appointment import ACTIVE_STATUSES, Appointment
from app.models.conversation import Conversation
from app.models.knowledge_base import KnowledgeBase
from app.models.lead import Lead, LeadStatus
from app.models.operator import Operator
from app.repositories.appointment import AppointmentRepository
from app.repositories.doctor import DoctorRepository
from app.services.appointment import (
    CLINIC_TIMEZONE,
    UNASSIGNED_DOCTOR_NAME,
    MissingPatientIdentityError,
    OutsideWorkingHoursError,
    SlotAlreadyBookedError,
    cancel_appointment,
    confirm_appointment,
    create_appointment,
)

router = APIRouter(prefix="/api/admin", tags=["Admin — Schedule"])

# How far back the dashboard's chart looks. Two weeks is what fits on a
# screen without becoming unreadable, and long enough to see a week-on-week
# change.
ANALYTICS_DAYS = 14


def _out(appointment: Appointment) -> AppointmentOut:
    return AppointmentOut(
        id=appointment.id,
        scheduled_at=appointment.scheduled_at,
        doctor_id=appointment.doctor_id,
        doctor_name=appointment.doctor_name,
        patient_name=appointment.patient_name,
        patient_phone=appointment.patient_phone,
        notes=appointment.notes,
        status=appointment.status,
        source=appointment.source,
    )


def _day_bounds_utc(local_date: date) -> tuple[datetime, datetime]:
    """[start, end) for local_date in clinic-local time, converted to UTC — a
    clinic's "day" is a local-calendar concept, the same reasoning as the
    working-hours logic in app.services.appointment.
    """
    start = datetime.combine(local_date, datetime.min.time(), tzinfo=CLINIC_TIMEZONE)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


@router.get("/appointments", response_model=list[AppointmentOut])
async def list_appointments(
    operator: Operator = Depends(get_current_operator),
    session: AsyncSession = Depends(get_db_session),
    day: date | None = Query(default=None, alias="date"),
) -> list[AppointmentOut]:
    selected = day or datetime.now(CLINIC_TIMEZONE).date()
    start, end = _day_bounds_utc(selected)
    appointments = await AppointmentRepository(session).list_between(start, end)
    return [_out(appointment) for appointment in appointments]


@router.post(
    "/appointments",
    response_model=AppointmentOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_csrf_header)],
)
async def create(
    payload: AppointmentCreate,
    operator: Operator = Depends(require_manage_role),
    session: AsyncSession = Depends(get_db_session),
) -> AppointmentOut:
    doctor_name = UNASSIGNED_DOCTOR_NAME
    if payload.doctor_id is not None:
        doctor = await DoctorRepository(session).get(payload.doctor_id)
        if doctor is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shifokor topilmadi")
        doctor_name = doctor.name

    repo = AppointmentRepository(session)
    try:
        appointment = await create_appointment(
            repo,
            scheduled_at=payload.scheduled_at,
            source="operator",
            patient_name=payload.patient_name,
            patient_phone=payload.patient_phone,
            doctor_id=payload.doctor_id,
            doctor_name=doctor_name,
            notes=payload.notes,
        )
    except SlotAlreadyBookedError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Bu vaqt allaqachon band"
        ) from None
    except OutsideWorkingHoursError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Bu vaqt ish vaqtidan tashqarida"
        ) from None
    except MissingPatientIdentityError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Bemor ismi kerak"
        ) from None

    await session.commit()
    return _out(appointment)


async def _load(session: AsyncSession, appointment_id: uuid.UUID) -> Appointment:
    appointment = await AppointmentRepository(session).get(appointment_id)
    if appointment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Qabul topilmadi")
    return appointment


@router.post(
    "/appointments/{appointment_id}/cancel",
    response_model=AppointmentOut,
    dependencies=[Depends(verify_csrf_header)],
)
async def cancel(
    appointment_id: uuid.UUID,
    operator: Operator = Depends(require_manage_role),
    session: AsyncSession = Depends(get_db_session),
) -> AppointmentOut:
    repo = AppointmentRepository(session)
    appointment = await _load(session, appointment_id)
    await cancel_appointment(repo, appointment)
    await session.commit()
    return _out(appointment)


@router.post(
    "/appointments/{appointment_id}/confirm",
    response_model=AppointmentOut,
    dependencies=[Depends(verify_csrf_header)],
)
async def confirm(
    appointment_id: uuid.UUID,
    operator: Operator = Depends(require_manage_role),
    session: AsyncSession = Depends(get_db_session),
) -> AppointmentOut:
    """Records that somebody spoke to the patient. The slot stays held —
    confirming does not change what the time is booked for.
    """
    repo = AppointmentRepository(session)
    appointment = await _load(session, appointment_id)
    await confirm_appointment(repo, appointment)
    await session.commit()
    return _out(appointment)


@router.get("/analytics", response_model=AnalyticsSummary)
async def analytics(
    operator: Operator = Depends(get_current_operator),
    session: AsyncSession = Depends(get_db_session),
) -> AnalyticsSummary:
    """The numbers the dashboard opens on.

    Every count carries its own tenant filter rather than going through a
    helper that reaches for `model.tenant_id`: four different tables are
    counted here, and a helper written that way would be trusting that all
    of them have the column — messages, for one, does not.
    """
    tenant_id = get_current_tenant()

    async def _count(*conditions: ColumnElement[bool]) -> int:
        stmt = select(func.count()).where(*conditions)
        return int((await session.execute(stmt)).scalar_one())

    since = datetime.now(UTC) - timedelta(days=ANALYTICS_DAYS)
    # Grouped in clinic-local time, not UTC: a booking at 01:00 Tashkent
    # belongs to that day on the clinic's own calendar, and grouping by the
    # UTC date would file it under the previous one.
    day_expr = func.date(func.timezone(str(CLINIC_TIMEZONE), Appointment.scheduled_at))
    by_day = (
        await session.execute(
            select(day_expr.label("day"), func.count().label("count"))
            .select_from(Appointment)
            .where(Appointment.tenant_id == tenant_id, Appointment.scheduled_at >= since)
            .group_by(day_expr)
            .order_by(day_expr)
        )
    ).all()

    return AnalyticsSummary(
        conversations_total=await _count(Conversation.tenant_id == tenant_id),
        conversations_open=await _count(
            Conversation.tenant_id == tenant_id, Conversation.status == "open"
        ),
        conversations_with_operator=await _count(
            Conversation.tenant_id == tenant_id, Conversation.is_bot_enabled.is_(False)
        ),
        appointments_total=await _count(Appointment.tenant_id == tenant_id),
        appointments_active=await _count(
            Appointment.tenant_id == tenant_id, Appointment.status.in_(ACTIVE_STATUSES)
        ),
        leads_total=await _count(Lead.tenant_id == tenant_id),
        leads_new=await _count(Lead.tenant_id == tenant_id, Lead.status == LeadStatus.NEW),
        faqs_active=await _count(
            KnowledgeBase.tenant_id == tenant_id, KnowledgeBase.is_active.is_(True)
        ),
        appointments_by_day=[DayCount(day=row.day, count=row.count) for row in by_day],
    )
