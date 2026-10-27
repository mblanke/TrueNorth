"""The feeds' settings (the installer's ``tn_feeds_*``, rendered into the env file).

* ``TN_FEEDS_MODE``: ``off`` (the default on a fresh install), ``online`` (the daily pull),
  or ``bundle`` (air-gapped: signed bundles imported through the API).
* ``TN_FEEDS_HTTP_PROXY``: the site's HTTP proxy for the pull (empty: direct). The pull
  never uses a proxy the environment sets by itself.
* ``TN_FEEDS_NVD_API_KEY`` (optional, never logged) and ``TN_FEEDS_NVD_MIN_CVSS`` are read by
  the NVD source (``feed_backends/__init__.py``).
"""

from __future__ import annotations

import os

MODES = ("off", "online", "bundle")


def mode() -> str:
    """The configured mode; anything unknown is ``off``."""
    value = os.getenv("TN_FEEDS_MODE", "").strip().lower() or "off"
    return value if value in MODES else "off"


def proxy() -> str | None:
    return os.getenv("TN_FEEDS_HTTP_PROXY", "").strip() or None
