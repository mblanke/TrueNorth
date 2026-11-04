"""TrueNorth range compiler.

Turns a declarative range definition (ranges/<ID>.yml) plus blueprints, the site
configuration and the template catalogue into a deterministic, hash-stamped plan
(build/<ID>/plan.json). Ansible consumes the plan; it never computes allocations.
"""

__version__ = "1.0.0"
