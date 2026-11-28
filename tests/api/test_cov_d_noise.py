# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""Removed in the #463 fix round.

``app.routers.noise`` scores far above the corrected bottom-10 (its whole-slice
coverage was already ~96%), so these tests were withdrawn to keep the selection
honest; the write gate refuses file deletion, so this stub stands in and the
integrator can simply ``git rm`` it.
"""
