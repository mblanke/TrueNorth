"""Email status shapes (published in docs/interfaces/openapi.json). No field ever carries
the relay password."""

from __future__ import annotations

from pydantic import BaseModel, Field

DOCS = 'install/README.md, "Email (SMTP)"'


class EmailRelayOut(BaseModel):
    """The relay as configured. Shown to the platform administrator only."""

    host: str
    port: int
    security: str = Field(..., description="starttls | tls | none")
    from_address: str
    username: str = Field("", description="The account the API signs in as; empty: no sign-in")
    signs_in: bool = Field(..., description="Whether a username and password are set (the password is never shown)")


class EmailStatusOut(BaseModel):
    """Whether the API sends email (roll-call onboarding, reminders, seat-forfeit notices,
    calendar invites). Every signed-in user may ask; only the platform administrator sees
    the relay."""

    configured: bool = Field(..., description="False: mail is only logged, nobody is emailed")
    docs: str = Field(DOCS, description="Where the installer's settings are described")
    relay: EmailRelayOut | None = Field(None, description="Platform administrator only")
    can_send_test: bool = Field(False, description="Whether the caller may POST /email/test")


class EmailTestOut(BaseModel):
    """The outcome of a test message to the caller's own address."""

    sent: bool = Field(..., description="The relay accepted it (delivery is proven only in the inbox)")
    to: str
    detail: str
