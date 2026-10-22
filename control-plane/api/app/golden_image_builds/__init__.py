"""Golden-image builds and promotion (ADR 0013 §2).

Importing this package registers ``golden_image_builds`` on ``Base.metadata``.
``service.py`` records builds the template kit registers and promotes one (audited);
``router.py`` lists builds and promotes; the worker pins every range VM to the promoted
build (``worker/template_pins.py``).
"""

from .models import GoldenImageBuild

__all__ = ["GoldenImageBuild"]
