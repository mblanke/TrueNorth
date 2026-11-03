"""The console grant table (#366)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID


class ConsoleGrant(Base):
    """Permission for one user to open one VM's console through ``/ws/console/<grant>``.

    Kept in the database, not in a process: the API runs several workers and the browser's
    socket may land on any of them. The URL carries a random 256-bit token; only its SHA-256
    is stored (``token_hash``), so a database read does not hand out live consoles. ``id`` is
    the row's own name, safe for logs and audit.

    ``expires_at`` bounds when a socket may be opened (about five minutes after issue). One
    socket at a time: ``live_until`` is a lease a connected relay renews every few seconds
    and clears on close (only the socket that holds it, ``live_by``, may renew or clear it); a relay
    that dies leaves it to lapse within seconds, not at expiry.
    ``view_only`` is the intent of the audience (an observer): the relay filters client input
    on it (S4); until then the hypervisor's own view-only support, as before. ``observed``
    mirrors the ticket's flag."""

    __tablename__ = "console_grants"
    __table_args__ = (Index("ix_console_grants_expires", "expires_at"), Index("ix_console_grants_user", "user_id"))

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    range_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("ranges.id", ondelete="CASCADE"), nullable=False)
    vm_id: Mapped[str] = mapped_column(String(255), nullable=False)
    vm_name: Mapped[str] = mapped_column(String(255), nullable=False)
    view_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    observed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Why it was issued, and what authorised it: re-checked at every connect and renewal
    # (policy.py). ``audience`` is student, observer or staff; a class console names its class
    # and session window, a lab-session console its lab session.
    audience: Mapped[str] = mapped_column(String(16), nullable=False, default="student")
    class_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    event_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    lab_session_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    dials: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # successful dials so far
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    live_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    live_by: Mapped[str | None] = mapped_column(String(32), nullable=True)  # the socket holding the claim
