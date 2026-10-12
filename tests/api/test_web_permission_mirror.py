"""The web app's copy of ROLE_PERMISSIONS must equal rbac.py's.

control-plane/web/src/app/core/auth/permissions.ts decides which menu entries, routes and
buttons each role sees. If it drifts from rbac.py, a role is shown pages the API refuses,
or denied pages it may open (R0-B: Authoring required instructor while range_ops held
range and template permissions). This parses the TS table between its mirror markers and
compares it with the Python one, role by role.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.rbac import ROLE_PERMISSIONS, Permission

TS = Path(__file__).resolve().parents[2] / "control-plane/web/src/app/core/auth/permissions.ts"
TOKEN = re.compile(r"'([a-z_]+:[a-z_]+)'")


COMMENTS = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)


def _strip_comments(text: str) -> str:
    """A commented-out ``// 'x:y'`` is not a grant (the markers are re-found first)."""
    return COMMENTS.sub("", text)


def _ts() -> str:
    return TS.read_text(encoding="utf-8")


def _block(text: str, start: str, end: str) -> str:
    return text[text.index(start) : text.index(end, text.index(start))]


def _web_table() -> dict[str, set[str] | str]:
    mirror = _strip_comments(_block(_ts(), "// mirror:start", "// mirror:end"))
    roles = list(re.finditer(r"^\s{2}([a-z_]+):", mirror, re.MULTILINE))
    out: dict[str, set[str] | str] = {}
    for i, m in enumerate(roles):
        body = mirror[m.end() : roles[i + 1].start() if i + 1 < len(roles) else len(mirror)]
        out[m.group(1)] = "ALL" if "ALL_PERMISSIONS" in body.split("\n")[0] else set(TOKEN.findall(body))
    return out


def test_every_permission_is_known_to_the_web_app():
    declared = set(TOKEN.findall(_strip_comments(_block(_ts(), "export const ALL_PERMISSIONS", "];"))))
    assert declared == {p.value for p in Permission}


def test_commented_out_grants_are_not_counted():
    text = "  student: [\n    'wiki:read',\n    // 'infra:read',\n    /* 'audit:read', */\n  ],\n"
    assert set(TOKEN.findall(_strip_comments(text))) == {"wiki:read"}


def test_each_role_matches_rbac():
    web = _web_table()
    assert set(web) == {r.value for r in ROLE_PERMISSIONS}
    for role, perms in ROLE_PERMISSIONS.items():
        expected = {p.value for p in perms}
        got = web[role.value]
        if got == "ALL":
            assert expected == {p.value for p in Permission}, f"{role.value}: web says all, rbac does not"
        else:
            assert got == expected, (
                f"{role.value}: web-only {sorted(got - expected)}, rbac-only {sorted(expected - got)}"
            )
