"""The site's time zone (``SITE_TIMEZONE``, IANA, default UTC).

One reading for every section that tells people a local time or a local day: the
self-serve lab gate's "next free at HH:MM" (#288) and the classes' day-before reminder
(#319). The installer sets it (#318); an unknown name is logged and read as UTC.
"""

from __future__ import annotations

import logging
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger("truenorth.api.site_time")


def site_timezone() -> ZoneInfo:
    name = os.getenv("SITE_TIMEZONE", "UTC").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("SITE_TIMEZONE=%r is not a time zone; using UTC", name)
        return ZoneInfo("UTC")
