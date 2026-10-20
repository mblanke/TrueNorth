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


class ApplicantQualificationOut(BaseModel):
    """A qualification an applicant may request (register) or a Student may pick (onboarding).

    Only what the dropdown shows. ``/qsp/qualifications`` carries more (NQUAL, PO count)
    and is for signed-in accounts.
    """

    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    qsp_code: str
    title: str
