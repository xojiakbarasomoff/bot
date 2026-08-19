import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError

from app.models.appointment import Appointment
from app.repositories.appointment import AppointmentRepository

# TODO(IGB-?): move onto Tenant.settings once the admin/dashboard panel
# exists, so each clinic can tune its own hours/slot length instead of every
# tenant sharing these — same pattern as
# core.config.Settings.debounce_window_seconds.
CLINIC_TIMEZONE = ZoneInfo("Asia/Tashkent")
SLOT_MINUTES = 30
WORK_START = time(9, 0)
WORK_END = time(19, 0)
DEFAULT_DOCTOR_NAME = "Dr. Aziza Karimova"  # placeholder until multi-doctor support exists
DEFAULT_SEARCH_HORIZON_DAYS = 14


class SlotAlreadyBookedError(Exception):
    """create_appointment() lost the race (or simply arrived second) for
    this exact slot. The DB's partial unique index is what actually
    enforces this — this exception is just the graceful, catchable form of
    the IntegrityError it raises.
    """

    def __init__(self, scheduled_at: datetime) -> None:
        self.scheduled_at = scheduled_at
        super().__init__(f"slot already booked: {scheduled_at.isoformat()}")


class OutsideWorkingHoursError(Exception):
    """scheduled_at falls outside working hours, or isn't aligned to the slot grid."""

    def __init__(self, scheduled_at: datetime) -> None:
        self.scheduled_at = scheduled_at
        super().__init__(f"outside working hours: {scheduled_at.isoformat()}")


class NoAvailabilityError(Exception):
    """No free slot was found anywhere in the search horizon."""

    def __init__(self, from_datetime: datetime, horizon_days: int) -> None:
        self.from_datetime = from_datetime
        self.horizon_days = horizon_days
        super().__init__(
            f"no free slot found within {horizon_days} days of {from_datetime.isoformat()}"
        )


class MissingPatientIdentityError(Exception):
    """create_appointment() was given neither user_id nor patient_name — a
    booking must be identifiable as *someone's* appointment.
    """

    def __init__(self) -> None:
        super().__init__("create_appointment requires at least one of user_id or patient_name")


def _to_local(scheduled_at: datetime) -> datetime:
    return scheduled_at.astimezone(CLINIC_TIMEZONE)


def is_within_working_hours(scheduled_at: datetime) -> bool:
    """Whether scheduled_at (any tz) lands on a bookable slot: inside
    [WORK_START, WORK_END) local clinic time, and aligned to the
    SLOT_MINUTES grid measured from WORK_START.

    A time that's merely *inside* working hours but off-grid (e.g. 9:17) is
    not a valid slot — the bot/dashboard only ever offer grid-aligned times,
    so an off-grid scheduled_at reaching here means the caller built it
    wrong.
    """
    local = _to_local(scheduled_at)
    if not (WORK_START <= local.time() < WORK_END):
        return False
    if local.second or local.microsecond:
        return False
    minutes_since_open = (local.hour * 60 + local.minute) - (
        WORK_START.hour * 60 + WORK_START.minute
    )
    return minutes_since_open % SLOT_MINUTES == 0


def _day_slots(local_date: date) -> Iterator[datetime]:
    """Every bookable local slot start on local_date, tz-aware in CLINIC_TIMEZONE."""
    current = datetime.combine(local_date, WORK_START, tzinfo=CLINIC_TIMEZONE)
    end = datetime.combine(local_date, WORK_END, tzinfo=CLINIC_TIMEZONE)
    step = timedelta(minutes=SLOT_MINUTES)
    while current < end:
        yield current
        current += step


def _first_slot_on_or_after(local_dt: datetime) -> datetime:
    """The first grid-aligned local slot start that is >= local_dt — rolls
    to the next day's opening slot if local_dt is after today's last slot.
    """
    for slot in _day_slots(local_dt.date()):
        if slot >= local_dt:
            return slot
    return next(iter(_day_slots(local_dt.date() + timedelta(days=1))))


async def check_availability(repo: AppointmentRepository, scheduled_at: datetime) -> bool:
    """Whether this exact slot could be booked right now.

    UX only — a True here is not a hold on the slot, so create_appointment()
    can still lose a race against a booking that lands between this check
    and the insert. That race is resolved by the DB's partial unique index,
    not by this function.
    """
    if not is_within_working_hours(scheduled_at):
        return False
    return await repo.get_active_at(scheduled_at) is None


async def find_next_free_slot(
    repo: AppointmentRepository,
    from_datetime: datetime,
    *,
    horizon_days: int = DEFAULT_SEARCH_HORIZON_DAYS,
) -> datetime:
    """The first free, working-hours, grid-aligned slot at or after
    from_datetime, searching up to horizon_days ahead.

    Fetches the whole window's busy slots in a single query, then walks the
    local slot grid in memory — not one query per candidate slot.
    """
    local_start = _first_slot_on_or_after(_to_local(from_datetime))
    last_day = local_start.date() + timedelta(days=horizon_days)
    # Bounded by the *end* of the last day checked (WORK_END), not
    # local_start's time-of-day N days out — otherwise the busy-set query
    # would cut off mid-afternoon on the last day and a real booking late in
    # the horizon could be missed, making an actually-taken slot look free.
    window_end_local = datetime.combine(last_day, WORK_END, tzinfo=CLINIC_TIMEZONE)

    busy = {
        appt.scheduled_at
        for appt in await repo.list_active_between(
            local_start.astimezone(UTC), window_end_local.astimezone(UTC)
        )
    }

    for day_offset in range(horizon_days + 1):
        day = local_start.date() + timedelta(days=day_offset)
        for slot in _day_slots(day):
            if slot < local_start:
                continue
            candidate_utc = slot.astimezone(UTC)
            if candidate_utc not in busy:
                return candidate_utc

    raise NoAvailabilityError(from_datetime, horizon_days)


async def create_appointment(
    repo: AppointmentRepository,
    *,
    scheduled_at: datetime,
    source: str,
    user_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
    patient_name: str | None = None,
    doctor: str = DEFAULT_DOCTOR_NAME,
) -> Appointment:
    """Books scheduled_at, refusing to double-book.

    user_id is optional: a bot booking always has one (there's no
    appointment without a prior IG conversation), but an operator booking a
    walk-in or phone patient who's never messaged the clinic has no User row
    to attach — patient_name is that booking's identity instead. At least
    one of the two is required, checked here rather than left as a DB-level
    surprise.

    The is_within_working_hours check up front just gives an obviously-wrong
    caller a clear error early. The actual double-booking guard is the
    partial unique index on (tenant_id, scheduled_at): a violation surfaces
    here as IntegrityError and is translated to SlotAlreadyBookedError so
    callers (bot/operator/dashboard) never see a raw DB error.
    """
    if user_id is None and not patient_name:
        raise MissingPatientIdentityError()
    if not is_within_working_hours(scheduled_at):
        raise OutsideWorkingHoursError(scheduled_at)
    try:
        # A dedicated SAVEPOINT for just this insert attempt: on
        # IntegrityError, SQLAlchemy rolls back to it automatically before
        # re-raising, undoing only this attempt. Rolling back the session
        # itself (session.rollback()) would instead unwind everything since
        # the session's own autobegin — including any earlier, already-
        # successful work in the same request/session — which is not what a
        # "this one slot was taken" error should do.
        async with repo.session.begin_nested():
            return await repo.create(
                user_id=user_id,
                scheduled_at=scheduled_at,
                status="scheduled",
                source=source,
                conversation_id=conversation_id,
                patient_name=patient_name,
                doctor=doctor,
            )
    except IntegrityError:
        raise SlotAlreadyBookedError(scheduled_at) from None


async def cancel_appointment(repo: AppointmentRepository, appointment: Appointment) -> Appointment:
    """Cancels a booking, which — via the partial unique index — immediately
    frees its slot for rebooking.
    """
    return await repo.update(appointment, status="cancelled")
