"""The exercise template: a reusable exercise (ADR 0017, docs/GLOSSARY.md).

Objectives, MESL, scenario, range template, teams and the instructor's notes live here once;
an exercise is created from it and keeps its own copy. Proven and portable through the
content port (``app.content_port``). Importing this package registers its tables on
``Base.metadata``.
"""

from .models import ExerciseTemplate, ExerciseTemplateUse

__all__ = ["ExerciseTemplate", "ExerciseTemplateUse"]
