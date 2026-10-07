"""Process logging setup, with opt-in JSON lines.

``LOG_FORMAT=json`` writes one JSON object per record (for a log shipper); anything
else keeps the plain text format the API has always used. ``LOG_LEVEL`` sets the
level either way.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

TEXT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# Attributes every LogRecord has. Anything else on a record came from `extra=` and is
# passed through as a field.
_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line: timestamp, level, logger, message, plus extras."""

    def format(self, record: logging.LogRecord) -> str:
        out = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD and not key.startswith("_"):
                out[key] = value
        if record.exc_info:
            out["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            out["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(out, default=str)


def configure_logging() -> None:
    """Configure the root logger from LOG_FORMAT / LOG_LEVEL.

    Text mode is exactly the previous ``logging.basicConfig`` call, so the default is
    unchanged (including basicConfig's no-op when handlers already exist).
    """
    level = os.getenv("LOG_LEVEL", "INFO")
    if os.getenv("LOG_FORMAT", "").strip().lower() != "json":
        logging.basicConfig(level=level, format=TEXT_FORMAT)
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
