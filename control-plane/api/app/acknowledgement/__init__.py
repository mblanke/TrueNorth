"""The acknowledgement form: the rules of use everyone signs (ADR 0014 decision 3, issue #279).

Importing this package registers its tables on ``Base.metadata``. ``rule`` decides who
must sign (``auth.get_current_user`` refuses everyone else with 403
``acknowledgement_required``); ``service`` publishes versions and records signatures.
"""

from .models import AcknowledgementSignature, AcknowledgementVersion

__all__ = ["AcknowledgementSignature", "AcknowledgementVersion"]
