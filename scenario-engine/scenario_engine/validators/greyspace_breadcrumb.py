"""Greyspace breadcrumb validator: did the team find this exercise's breadcrumb (ADR 0007)?

A breadcrumb's ``{{ token }}`` is keyed per exercise by the installation's secret
(injectors/greyspace_breadcrumb.py ``breadcrumb_token``), so this validator recomputes
it rather than reading it back from anywhere. Params: ``crumb_id`` (required), ``salt``
(optional, as the injector's). Context: ``exercise_id`` and ``answer`` (what the team
submitted: a flag field, a report excerpt). True when the token appears in the answer.
Without a breadcrumb key it raises ``BreadcrumbKeyError`` rather than judge.

The scoring engine's form of the same check is ``ScoringValidator`` method
``greyspace_breadcrumb`` (scoring/validators.py). That the breadcrumb was planted and is
still served is the worker's check (``bin/gs crumb check`` on the Greyspace host).
"""

from __future__ import annotations

from typing import Any

from ..injectors.greyspace_breadcrumb import crumb_found
from .base import BaseValidator


class GreyspaceBreadcrumbValidator(BaseValidator):
    def validate(self, context: dict[str, Any]) -> bool:
        return crumb_found(
            str(context.get("exercise_id") or ""),
            str(self.params.get("crumb_id") or ""),
            str(context.get("answer") or ""),
            self.params.get("salt"),
        )
