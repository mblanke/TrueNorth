"""Lab access tokens: what a student's browser carries after an LTI launch from Moodle.

An LTI launch proves who the student is to the API, but the browser it redirects has no
TrueNorth login. The launch therefore hands the lab page a token for that one session:
signed with the TrueNorth tool key, audience ``truenorth-lab``, subject the session id,
valid no longer than the lab itself. It opens that session's status, console, reset and
end, and nothing else.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime

import jwt
from cryptography.hazmat.primitives import serialization
from sqlalchemy.orm import Session

from .. import lti13

AUDIENCE = "truenorth-lab"
MAX_SECONDS = 12 * 3600


class LabTokenError(ValueError):
    pass


def mint(db: Session, session_id: uuid.UUID, user_id: uuid.UUID, expires_at: datetime | None) -> str:
    now = int(time.time())
    until = int(expires_at.timestamp()) if expires_at else now + MAX_SECONDS
    key = lti13.get_tool_key(db)
    claims = {
        "iss": "truenorth",
        "typ": "lab",  # never accepted as any other kind of TrueNorth-signed token
        "aud": AUDIENCE,
        "sub": str(session_id),
        "uid": str(user_id),
        "iat": now,
        "exp": min(until, now + MAX_SECONDS),
    }
    return jwt.encode(claims, key.private_key_pem, algorithm="RS256", headers={"kid": key.kid})


def verify(db: Session, token: str, session_id: uuid.UUID) -> dict:
    key = lti13.get_tool_key(db)
    public = serialization.load_pem_public_key(key.public_key_pem.encode())
    try:
        claims = jwt.decode(token, public, algorithms=["RS256"], audience=AUDIENCE, issuer="truenorth")
    except jwt.PyJWTError as exc:
        raise LabTokenError(f"lab token refused: {exc}") from exc
    if claims.get("typ") != "lab" or claims.get("sub") != str(session_id):
        raise LabTokenError("this token is for another lab")
    return claims
