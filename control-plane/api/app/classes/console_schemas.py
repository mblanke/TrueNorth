"""Wire shapes of a class's consoles and its live view (``console_router.py``; #313, #321)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

MACHINE = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$"


class ConsoleMachineOut(BaseModel):
    machine: str = Field(description="The template node id: what the range's accounts name the machine. Several "
                                     "VMs share it when the template node has a count above one")
    name: str = Field(description="The VM's name, unique in its range: a console is opened by it")
    power: str | None = Field(None, description="The VM's state as its last build or check recorded it")
    student_machine: bool = Field(False, description="A Student is handed an account on it; observers open "
                                                     "consoles on these only")


class MyRangeConsolesOut(BaseModel):
    """The machines of a class range the Student may open a console on, and whether now."""

    range_id: uuid.UUID
    open: bool = Field(description="A session of the class using this range is live, or within its "
                                   "provisioning lead: POST opens a console")
    session_id: uuid.UUID | None = Field(None, description="That session, when open")
    machines: list[ConsoleMachineOut] = Field(
        description="Only machines the Student is handed accounts on (as their credentials panel)")


class ClassConsoleOut(BaseModel):
    """A one-time console ticket. Short-lived and single-use; never a standing credential."""

    range_id: uuid.UUID
    machine: str = Field(description="The template node id")
    vm: str = Field(description="The VM's name")
    kind: str = Field(description="webmks (vSphere) or mock")
    url: str
    expires_in: int
    view_only: bool = Field(False, description="The hypervisor refuses input on this console")
    observed: bool = Field(False, description="An observer's console the hypervisor cannot make view-only: "
                                              "input reaches the machine, so the page says it is observed")


class ClassConsoleIn(BaseModel):
    range_id: uuid.UUID
    vm: str = Field(pattern=MACHINE, description="The VM's name (``ConsoleMachineOut.name``)")


class LiveRangeOut(BaseModel):
    range_id: uuid.UUID
    name: str
    state: str = Field(description="The range's own state (ready, running, stopped, provisioning, failed, ...)")
    machines: list[ConsoleMachineOut]


class LiveTeamOut(BaseModel):
    id: uuid.UUID
    name: str


class LiveStudentOut(BaseModel):
    entry_id: uuid.UUID
    user_id: uuid.UUID | None = None
    display_name: str = Field(description="Their name, or their roll-call email when they have no account yet")
    team: LiveTeamOut | None = None
    range: LiveRangeOut | None = Field(None, description="Their own seat (an individual session)")
    last_seen_at: datetime | None = Field(
        None, description="The last time they opened their range (accounts, a password or a console) since the "
                          "session's provisioning lead")


class ClassLiveOut(BaseModel):
    """A class session's live view (ADR 0014 §19): each Student's machines and status."""

    class_id: uuid.UUID
    class_name: str
    event_id: uuid.UUID
    session_name: str
    start_time: datetime
    end_time: datetime
    range_mode: str
    role: str = Field(description="The caller's part in the class: lead, assistant, observer or admin")
    live: bool = Field(description="The session is under way now")
    consoles_open: bool = Field(description="Consoles can be opened now (the session's window)")
    consoles_for_you: bool = Field(
        description="Your part may open consoles at all: false for an observer while CLASS_OBSERVER_CONSOLES is "
                    "off (the status grid only)")
    view_only_consoles: bool = Field(
        description="An observer's console is view-only on this hypervisor; otherwise it is marked observed")
    shared_range: LiveRangeOut | None = Field(None, description="The range a collective session books")
    students: list[LiveStudentOut]
    generated_at: datetime
