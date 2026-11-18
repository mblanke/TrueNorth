"""Docs that promise what the code does not have (R0-B, audit 2).

* docs/user-guide.md and the role guides (docs/guides/) may name a UI feature only if the
  code has it. The user guide's "Not in v1.2.0" section is where absent features are
  listed, so it is exempt.
* Every repository path cited as evidence in docs/security.md's compliance mapping exists.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs/user-guide.md"
ROLE_GUIDES = sorted((ROOT / "docs/guides").glob("*.md"))
SECURITY = ROOT / "docs/security.md"
WEB = ROOT / "control-plane/web/src/app"
OPENAPI = ROOT / "docs/interfaces/openapi.json"


def _without_section(text: str, heading: str) -> str:
    """Drop one '## ' section (heading to the next '## ')."""
    start = text.find(f"\n## {heading}\n")
    if start < 0:
        return text
    end = text.find("\n## ", start + 1)
    return text[:start] + (text[end:] if end >= 0 else "")


def _web_source() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in WEB.rglob("*.ts") if not p.name.endswith(".spec.ts"))


def test_user_guide_names_only_features_the_code_has():
    guide = _without_section(GUIDE.read_text(encoding="utf-8"), "Not in v1.2.0")
    guide += "\n".join(p.read_text(encoding="utf-8") for p in ROLE_GUIDES)
    web = _web_source()
    api_paths = OPENAPI.read_text(encoding="utf-8").lower()
    claims = {
        # phrase in the guide: whether the code has it
        "Team Standings": "Team Standings" in web,
        "Score Summary": "Score Summary" in web,
        "command palette": "command palette" in web.lower(),
        "G then": "document:keydown" in (WEB / "app.component.ts").read_text(encoding="utf-8"),
        "Ctrl + K": "document:keydown" in (WEB / "app.component.ts").read_text(encoding="utf-8"),
        "leaderboard": "leaderboard" in api_paths,
    }
    promised = [p for p, exists in claims.items() if p.lower() in guide.lower() and not exists]
    assert not promised, f"the user guides promise features the code lacks: {promised}"


def test_user_guide_lists_what_is_missing():
    assert "\n## Not in v1.2.0\n" in GUIDE.read_text(encoding="utf-8")


def test_the_rc_upgrade_notes_exist_and_state_the_migration_fact():
    """06 section 2.1 points at per-release notes; rc21's say whether a migration is needed
    (last-mile 1.6). Presence only: the fact itself was checked by diffing the two tags."""
    notes = (ROOT / "docs/release-notes/v1.2.0-rc21.md").read_text(encoding="utf-8")
    assert "no migration" in notes.lower() and "rc19" in notes and "rc20" in notes
    assert "v1.2.0-rc21.md" in (ROOT / "docs/guides/06-operations-runbook.md").read_text(encoding="utf-8")


def test_the_role_guides_are_checked():
    names = {p.name for p in ROLE_GUIDES}
    assert {"03-admin-first-day.md", "04-instructor-guide.md", "05-student-quick-start.md"} <= names


def test_security_compliance_evidence_paths_exist():
    text = SECURITY.read_text(encoding="utf-8")
    section = text[text.index("## Compliance Mapping") :]
    cited = set(re.findall(r"`((?:control-plane|infra|install|scripts|docs|tests|ai-orchestrator|\.github)/[^`\s]+)`", section))
    assert cited, "no evidence paths found in the compliance mapping"
    missing = sorted(p for p in cited if not (ROOT / p.rstrip("/")).exists())
    assert not missing, f"docs/security.md cites paths that do not exist: {missing}"


def test_the_guides_agree_on_who_signs_in_first():
    """The bootstrap administrator is the local break-glass account; the first AD admin is
    approved afterwards (02 section 7, install/README.md). Guides once said the first sign-in
    is a named AD account (#487, last-mile 1.1)."""
    readme = (ROOT / "install/README.md").read_text(encoding="utf-8")
    guides = {p.name: p.read_text(encoding="utf-8") for p in ROLE_GUIDES}
    guides["install/README.md"] = readme
    stale = [
        (name, phrase)
        for name, text in guides.items()
        for phrase in ("NAMED AD account", "The named AD account that admits everyone else")
        if phrase in text
    ]
    assert not stale, f"these still say the first sign-in is an AD account: {stale}"
    for name in ("02-install-guide.md", "03-admin-first-day.md"):
        assert "break-glass" in guides[name], f"{name} must name the break-glass bootstrap account"
    assert "break-glass" in readme


def test_the_clean_room_protocol_has_both_phases():
    """A site with no test site cannot finish the install in one pass: Phase A (mock
    provisioner) needs nothing from the owner, Phase B (real vCenter) needs ported templates
    and the build passwords. The protocol must say so, and every placeholder its commands use
    must be defined where the guide that holds them says (#487, last-mile 1.2)."""
    clean = (ROOT / "docs/guides/clean-room-install.md").read_text(encoding="utf-8")
    for phrase in ("Phase A", "Phase B", "tn_provisioner_backend: mock", "Phase B prerequisites"):
        assert phrase in clean, f"clean-room-install.md must mention {phrase!r}"
    install = (ROOT / "docs/guides/02-install-guide.md").read_text(encoding="utf-8")
    ops = (ROOT / "docs/guides/06-operations-runbook.md").read_text(encoding="utf-8")
    for placeholder in ("<range-host-N>", "<non-range-port-group>"):
        assert placeholder in ops, f"06 section 4 no longer uses {placeholder}: drop the row from 02"
        assert placeholder in install, f"02's site-values table must define {placeholder}"
    assert "tn_provisioner_backend: mock" in install
