"""Every ORM model, including the per-section modules kept out of models.py.

ADR 0003 caps models.py; a section's models live in their own module (models_wiki,
models_tickets, ...). Importing this module registers all of them with ``Base``, which
is what anything working on the whole schema needs: Alembic, ``create_all`` and the
migration-completeness checks. Code that needs a section's models imports that module.
"""

# Core models first (alphabetical order happens to give that): the sections refer to them.
from . import models, models_tickets, models_wiki  # noqa: F401
from .db import Base

__all__ = ["Base"]
