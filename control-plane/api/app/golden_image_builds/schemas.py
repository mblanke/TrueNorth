"""Request and response bodies of golden-image builds (ADR 0013 §2)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

HostName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")]
CopyName = Annotated[str, StringConstraints(pattern=r"^[^@\s]{1,100}@[A-Za-z0-9_-]{1,63}-\d{8}$")]


class GoldenImageBuildIn(BaseModel):
    """A build the template kit registers (``build.sh`` / ``build-phase.sh register``).

    ``copies`` names the build's copy on each range host, keyed by the host's short name
    (``{"esx03": "srv2022@esx03-20261021"}``). Registering the same ``build_id`` again
    adds copies for new hosts; a promoted build never changes."""

    build_id: str = Field(..., pattern=r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,119}$")
    checksum: str = Field(..., pattern=r"^[0-9a-f]{64}$", description="sha256 (hex) of the build manifest")
    build_date: date | None = None
    iso_checksum: str = Field("", max_length=160, pattern=r"^[A-Za-z0-9:._-]*$")
    template_name: str = Field("", max_length=120)
    copies: dict[HostName, CopyName] = Field(default_factory=dict, max_length=64)


class GoldenImageBuildOut(BaseModel):
    id: str
    build_id: str
    template_name: str
    checksum: str
    build_date: date | None
    iso_checksum: str
    copies: dict[str, str]
    registered_by: str
    registered_at: datetime | None
    promoted: bool
    promoted_at: datetime | None
    promoted_by: str
    # Whether an admin may promote it: it must name at least one host copy (ADR 0013 §2).
    promotable: bool = False
    not_promotable_reason: str = ""
    # Discovered vSphere hosts with no copy of this build: placement will not use them for
    # this image once it is promoted. Empty in image listings.
    missing_hosts: list[str] = Field(default_factory=list)


class GoldenImageBuildPromoteIn(BaseModel):
    """Why this build is being promoted; kept in the audit entry."""

    reason: str = Field("", max_length=500)
