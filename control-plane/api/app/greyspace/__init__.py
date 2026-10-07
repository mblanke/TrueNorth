"""Greyspace: a simulated internet a range can attach (ADR 0007, docs/greyspace-plan.md).

``manifest``, ``fixture`` and ``config`` are pure standard library so the runtime
builder in ``greyspace/scripts`` can import them without the API's database. The table
lives in ``models`` and is registered by importing that module (``app.main``,
``alembic/env.py``), not this package.
"""
