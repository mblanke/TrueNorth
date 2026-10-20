"""Registration-section schemas kept out of schemas.py (ADR 0003)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict


class ApplicantNationOut(BaseModel):
    """One nation as an applicant sees it on the registration and onboarding forms.

    Deliberately narrower than ``NationOut`` (``/directory/nations``): the public
    identifying fields only, no alliance flags and no ``is_active`` (inactive rows are
    never returned).
    """

    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    iso_alpha2: str
    iso_alpha3: str
    flag_emoji: str
