"""ARC² — AI Rapid Course Creator, adapted to TrueNorth.

A run lives under ``build/arc2/<slug>/``. Seven agents each write one ``NN-*/fragment.json``
holding only the manifest keys they own; ``arc2.check`` merges the fragments into
``manifest.json``, enforces the golden thread (module objective → PO → critical event →
validator), records the two human gates and prints the run status.

    python -m arc2.check init   build/arc2/<slug> --slug <slug> --request-file <txt>
    python -m arc2.check merge  build/arc2/<slug> <stage>
    python -m arc2.check check  build/arc2/<slug> [--json]
    python -m arc2.check gate   build/arc2/<slug> outline|preview accept|feedback [--text ..] [--route <stage>]
    python -m arc2.check status build/arc2/<slug>

Design: ``docs/AGENTS.md`` (ARC² section) and the plan it cites.
"""

__version__ = "0.1.0"
