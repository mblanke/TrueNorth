"""Pattern-of-life noise engine.

Synthetic personas whose daily routines produce real background traffic inside a range,
so the attack is not the only thing on the wire. One dial (0-100) sets how loud that is.

The in-VM agent that executes the plans is white-cell infrastructure: it is out of
bounds for students, talks to the controller only over the management network, and
its ground-truth activity log is never exposed to anyone without NOISE_READ.

This package is its own section: models, the dial, the planner and the canned roster
live here, the HTTP surface in ``app/routers/noise.py``.
"""

from . import models  # noqa: F401 — register the tables on the shared Base
