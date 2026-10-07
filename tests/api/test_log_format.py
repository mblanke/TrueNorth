"""LOG_FORMAT=json emits one JSON object per line; the default stays plain text."""

from __future__ import annotations

import io
import json
import logging

import pytest
from app import log_format
from app.log_format import JsonFormatter, configure_logging


@pytest.fixture
def root_logger():
    """Restore the root logger's handlers and level after each test."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield root
    root.handlers[:] = handlers
    root.setLevel(level)


def _record(msg="hello %s", args=("world",), **extra):
    rec = logging.LogRecord("truenorth.test", logging.WARNING, __file__, 1, msg, args, None)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_json_formatter_fields():
    out = json.loads(JsonFormatter().format(_record(request_id="abc")))
    assert out["level"] == "WARNING"
    assert out["logger"] == "truenorth.test"
    assert out["message"] == "hello world"
    assert out["request_id"] == "abc"
    assert out["timestamp"].endswith("+00:00")
    assert "args" not in out and "msg" not in out


def test_json_formatter_includes_exceptions():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    out = json.loads(JsonFormatter().format(rec))
    assert "ValueError: boom" in out["exc_info"]


def test_unserialisable_extras_do_not_break_logging():
    out = json.loads(JsonFormatter().format(_record(thing=object())))
    assert out["thing"].startswith("<object")


def test_log_format_json_installs_the_json_formatter(monkeypatch, root_logger):
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    configure_logging()
    assert len(root_logger.handlers) == 1
    handler = root_logger.handlers[0]
    assert isinstance(handler.formatter, JsonFormatter)
    assert root_logger.level == logging.DEBUG

    buf = io.StringIO()
    handler.setStream(buf)
    logging.getLogger("truenorth.api").info("started", extra={"tenant": "t1"})
    line = json.loads(buf.getvalue().strip())
    assert line["message"] == "started" and line["tenant"] == "t1"


def test_default_is_unchanged_text(monkeypatch, root_logger):
    """Without LOG_FORMAT it is the same basicConfig call main.py always made."""
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    calls = []
    monkeypatch.setattr(log_format.logging, "basicConfig", lambda **kw: calls.append(kw))
    configure_logging()
    assert calls == [{"level": "INFO", "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s"}]
