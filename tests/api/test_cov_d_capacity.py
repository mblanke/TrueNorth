# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""Removed in the #463 fix round.

``app.scheduler.capacity`` measured ~99% under the whole slice, which is what exposed
the earlier return's chain-specific numbers as unreproducible; it is not among the
corrected bottom-10, so these tests were withdrawn. The write gate refuses file
deletion, so this stub stands in and the integrator can simply ``git rm`` it.
"""
