"""The range lease table (CR1-06, CR1-11 in docs/review/codereview1.md)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID


class RangeLease(Base):
    """Which worker execution is acting on a range right now (worker/fencing.py).

    A range task takes this lease before it touches the hypervisor. A second copy that
    arrives while the first holds it is re-queued and comes back to find the range
    finished (it then skips) or the lease expired (it then takes over). The holder is
    one execution: ``<action>:<random token>`` (not the Celery task id, which a
    redelivered copy shares). Released when the task ends, however it ends, except after
    a soft time limit, when the hypervisor call may still be running. While the task runs
    a heartbeat renews ``expires_at`` a few minutes ahead, so a worker that died holds it
    only minutes. Abandoning the range operation renames the lease of that operation's
    action to a short tombstone, ``abandoned:<holder>`` (app/range_ops/service.py,
    fence_lease): the worker, if alive, is fenced out and renews the tombstone until its
    in-flight work ends; if dead, the tombstone expires within minutes. Expiry is
    written and compared in database time.
    """

    __tablename__ = "range_leases"

    range_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("ranges.id", ondelete="CASCADE"), primary_key=True)
    holder: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
