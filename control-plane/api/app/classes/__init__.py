"""Classes and roll call (ADR 0014, issue #278).

Importing this package registers its tables on ``Base.metadata``. Other sections use
``service.class_roll_call_emails`` (who a class's invites go to) and
``onboarding.admit_from_roll_call`` (roll call is approval, called by ``GET /auth/me``).
"""

from .models import Class, ClassSession, ClassStaff, RollCallEntry, RollCallSettings

__all__ = ["Class", "ClassSession", "ClassStaff", "RollCallEntry", "RollCallSettings"]
