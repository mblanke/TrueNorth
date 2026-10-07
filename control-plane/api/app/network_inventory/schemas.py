"""API shapes for network reservations (GET /ranges/{id}/network-reservations)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class NetworkReservationOut(BaseModel):
    """One address or VLAN a range holds on a shared network."""

    model_config = ConfigDict(from_attributes=True)
    domain: str
    kind: str
    value: str
    holder: str
