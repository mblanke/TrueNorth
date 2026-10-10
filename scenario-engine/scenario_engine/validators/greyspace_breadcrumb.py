"""Greyspace breadcrumb validator: did the team find this exercise's breadcrumb (ADR 0007)?

A breadcrumb's ``{{ token }}`` is derived from the exercise and the crumb id
(injectors/greyspace_breadcrumb.py ``breadcrumb_token``), so this validator recomputes
it rather than reading it back from anywhere. Params: ``crumb_id`` (required), ``salt``
(optional, as the injector's). Context: ``exercise_id`` and ``answer`` (what the team
submitted: a flag field, a report excerpt). True when the token appears in the answer.

That the breadcrumb was planted and is still served is the worker's check
(``bin/gs crumb check`` on the Greyspace host), not this validator's.
"""

from __future__ import annotations

from typing import Any

from ..injectors.greyspace_breadcrumb import breadcrumb_token
from .base import BaseValidator


class GreyspaceBreadcrumbValidator(BaseValidator):
    def validate(self, context: dict[str, Any]) -> bool:
        crumb = str(self.params.get("crumb_id") or "")
        exercise = str(context.get("exercise_id") or "")
        answer = str(context.get("answer") or "")
        if not crumb or not exercise or not answer:
            return False
        return breadcrumb_token(exercise, crumb, self.params.get("salt")).lower() in answer.lower()
