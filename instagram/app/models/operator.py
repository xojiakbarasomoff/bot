import uuid

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Operator(Base):
    # Clinic staff accounts (not patients — see User for patients). Both
    # "doctor" (view-only) and "operator" (view + book/cancel) dashboard
    # roles are rows here — see app.api.auth for the login/permission logic.
    __tablename__ = "operators"
    # Globally unique, not per-tenant: the login form is just
    # username+password with no separate clinic/tenant selector, so the
    # username alone must resolve to exactly one operator (and thus one
    # tenant) — see app.api.auth.login_submit.
    __table_args__ = (UniqueConstraint("username", name="uq_operators_username"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(50), nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    # bcrypt hash (app.core.passwords) — never plaintext, same posture as
    # Channel.credentials, just a one-way hash instead of reversible
    # encryption since nothing ever needs the plaintext password back.
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
