import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import (
    get_current_operator,
    get_current_session,
    require_operator_role,
    verify_csrf,
)
from app.core.db import get_db_session
from app.core.session import Session
from app.models.appointment import Appointment
from app.models.operator import Operator
from app.models.user import User
from app.repositories.appointment import AppointmentRepository
from app.services.appointment import (
    CLINIC_TIMEZONE,
    MissingPatientIdentityError,
    OutsideWorkingHoursError,
    SlotAlreadyBookedError,
    cancel_appointment,
    create_appointment,
)

templates = Jinja2Templates(directory="app/templates")

router = APIRouter(prefix="/dashboard")


@dataclass(frozen=True)
class AppointmentRow:
    """Presentation-only view of an Appointment for dashboard.html — local
    time already formatted and a display name already resolved, so the
    template has no timezone or fallback logic of its own.
    """

    id: uuid.UUID
    time: str
    patient_name: str
    status: str
    source: str


async def _appointment_rows(
    db_session: AsyncSession, appointments: list[Appointment]
) -> list[AppointmentRow]:
    # patient_name is only ever set on operator-booked appointments (the
    # dashboard's own create form always fills it) — a bot-booked
    # appointment (app.services.debounce / the future booking flow) has a
    # user_id but no patient_name, so its display name comes from the
    # linked User instead. One query for every user in the day, not one per
    # row.
    user_ids = {appt.user_id for appt in appointments if appt.user_id is not None}
    user_names: dict[uuid.UUID, str | None] = {}
    if user_ids:
        result = await db_session.execute(select(User.id, User.name).where(User.id.in_(user_ids)))
        user_names = dict(result.tuples().all())

    rows = []
    for appt in appointments:
        linked_name = user_names.get(appt.user_id) if appt.user_id else None
        display_name = appt.patient_name or linked_name or "Unknown"
        rows.append(
            AppointmentRow(
                id=appt.id,
                time=appt.scheduled_at.astimezone(CLINIC_TIMEZONE).strftime("%H:%M"),
                patient_name=display_name,
                status=appt.status,
                source=appt.source,
            )
        )
    return rows


def _today_in_clinic_tz() -> date:
    return datetime.now(CLINIC_TIMEZONE).date()


def _day_bounds_utc(local_date: date) -> tuple[datetime, datetime]:
    """[start, end) for local_date in clinic-local wall-clock time, converted
    to UTC — a clinic's "day" is a local-calendar concept, same reasoning as
    the working-hours logic in app.services.appointment.
    """
    start = datetime.combine(local_date, datetime.min.time(), tzinfo=CLINIC_TIMEZONE)
    end = start + timedelta(days=1)
    return start.astimezone(UTC), end.astimezone(UTC)


@router.get("/appointments", response_class=HTMLResponse)
async def day_view(
    request: Request,
    date_: date | None = Query(default=None, alias="date"),
    operator: Operator = Depends(get_current_operator),
    session: Session = Depends(get_current_session),
    db_session: AsyncSession = Depends(get_db_session),
    error: str | None = None,
) -> HTMLResponse:
    selected_date = date_ or _today_in_clinic_tz()
    start, end = _day_bounds_utc(selected_date)

    repo = AppointmentRepository(db_session)
    appointments = await repo.list_between(start, end)
    rows = await _appointment_rows(db_session, list(appointments))

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "operator": operator,
            "selected_date": selected_date,
            "today": _today_in_clinic_tz(),
            "appointments": rows,
            "clinic_timezone": CLINIC_TIMEZONE,
            "can_manage": operator.role == "operator",
            "csrf_token": session.csrf_token,
            "error": error,
        },
    )


@router.post("/appointments", dependencies=[Depends(verify_csrf)])
async def create_appointment_route(
    patient_name: str = Form(...),
    appointment_date: date = Form(..., alias="date"),
    appointment_time: str = Form(..., alias="time"),
    operator: Operator = Depends(require_operator_role),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    hour, _, minute = appointment_time.partition(":")
    try:
        local_dt = datetime(
            appointment_date.year,
            appointment_date.month,
            appointment_date.day,
            int(hour),
            int(minute),
            tzinfo=CLINIC_TIMEZONE,
        )
    except ValueError:
        return _redirect_to_day(appointment_date, error="Invalid time")
    scheduled_at = local_dt.astimezone(UTC)

    repo = AppointmentRepository(db_session)
    try:
        await create_appointment(
            repo,
            scheduled_at=scheduled_at,
            source="operator",
            patient_name=patient_name.strip(),
        )
    except SlotAlreadyBookedError:
        return _redirect_to_day(appointment_date, error="That slot is already booked")
    except OutsideWorkingHoursError:
        return _redirect_to_day(appointment_date, error="That time is outside working hours")
    except MissingPatientIdentityError:
        return _redirect_to_day(appointment_date, error="Patient name is required")

    await db_session.commit()
    return _redirect_to_day(appointment_date)


@router.post("/appointments/{appointment_id}/cancel", dependencies=[Depends(verify_csrf)])
async def cancel_appointment_route(
    appointment_id: str,
    redirect_date: date = Form(..., alias="date"),
    operator: Operator = Depends(require_operator_role),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    repo = AppointmentRepository(db_session)
    appointment = await repo.get(_parse_uuid_or_404(appointment_id))
    if appointment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appointment not found")

    await cancel_appointment(repo, appointment)
    await db_session.commit()
    return _redirect_to_day(redirect_date)


def _parse_uuid_or_404(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Appointment not found"
        ) from None


def _redirect_to_day(selected_date: date, *, error: str | None = None) -> RedirectResponse:
    url = f"/dashboard/appointments?date={selected_date.isoformat()}"
    if error:
        url += f"&error={quote(error)}"
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)
