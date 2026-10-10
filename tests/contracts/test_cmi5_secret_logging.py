"""A cmi5 launch URL's one-time fetch secret is never logged or sent on as a Referer.

The AU URL TrueNorth (or any LMS) launches carries ``fetch=<api>/cmi5/fetch/<secret>`` in
its query (cmi5 8.1). Review low 6 (2026-10-09): the edge and the web container logged the
request line and the Referer as they came, and pages opened from the AU sent the whole URL
on as their Referer. Now both nginx configs log a copy with the ``fetch`` value replaced,
and serve ``/au/`` with ``Referrer-Policy: no-referrer`` (the SPA also takes the launch out
of the address bar before its first request: features/cmi5/au-runtime.component.ts). These
check the configuration; the behaviour was confirmed against nginx:1.30-alpine itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EDGE_MAIN = ROOT / "infra/platform/nginx/nginx.conf"
EDGE_COMMON = ROOT / "infra/platform/nginx/snippets/security-headers-common.conf"
WEB = ROOT / "control-plane/web/nginx.conf"
SECRET = "c9Tx_gO_w6Ht165o7Jc13neYH0GLuO4hob3iHYccNg0"
LAUNCH = (
    "GET /au/releases/r/0?endpoint=https%3A%2F%2Ftn%2Fapi%2Fcmi5%2Flrs%2F"
    f"&fetch=https%3A%2F%2Ftn%2Fapi%2Fcmi5%2Ffetch%2F{SECRET}&actor=%7B%7D HTTP/1.1"
)


def _map(text: str, var: str) -> tuple[str, str]:
    """The (regex, replacement) of the one-pattern map that sets ``var``."""
    block = re.search(r"map \$\S+ \$" + var + r" \{(.*?)\n\s*\}", text, re.S)
    assert block, f"no map for ${var}"
    pattern, replacement = re.search(r'"~(.+?)"\s+"(.+?)";', block.group(1)).groups()
    return pattern, replacement


def _apply(pattern: str, replacement: str, value: str) -> str:
    m = re.match(pattern.replace("(?<", "(?P<"), value)
    return re.sub(r"\$\{(\w+)\}", lambda g: m.group(g.group(1)), replacement) if m else value


@pytest.mark.parametrize("conf", [EDGE_MAIN, WEB], ids=["edge", "web"])
def test_the_logged_request_and_referer_hide_the_fetch_secret(conf):
    text = conf.read_text()
    for var, sample in (("tn_request_logged", LAUNCH), ("tn_referer_logged", LAUNCH.split(" ")[1])):
        logged = _apply(*_map(text, var), sample)
        assert SECRET not in logged and "fetch=<redacted>" in logged, logged
        assert "actor=" in logged  # the rest of the line is kept


def test_the_edge_logs_only_the_redacted_copies():
    fmt = re.search(r"log_format json_combined.*?;", EDGE_MAIN.read_text(), re.S).group(0)
    assert "$tn_request_logged" in fmt and "$tn_referer_logged" in fmt
    assert '$request"' not in fmt and '$http_referer"' not in fmt


def test_the_web_container_logs_only_the_redacted_copies():
    text = WEB.read_text()
    assert re.search(r"access_log\s+\S+\s+tn_redacted;", text)
    fmt = re.search(r"log_format tn_redacted.*?;", text, re.S).group(0)
    assert "$tn_request_logged" in fmt and "$tn_referer_logged" in fmt


@pytest.mark.parametrize(("conf", "var"), [(EDGE_MAIN, "tn_referrer_policy"), (WEB, "tn_referrer")])
def test_module_pages_send_no_referer(conf, var):
    block = re.search(r"map \$request_uri \$" + var + r" \{(.*?)\}", conf.read_text(), re.S)
    assert block and re.search(r'"?~\^/au/"?\s+"no-referrer"', block.group(1)), "no no-referrer for /au/"
    if conf is EDGE_MAIN:
        assert re.search(r"add_header Referrer-Policy\s+\$tn_referrer_policy", EDGE_COMMON.read_text())
