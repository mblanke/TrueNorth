# Author: Marc-Andre Blanke
# Created: 2026-10-12T14:37:29Z
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
    rc22 = (ROOT / "docs/release-notes/v1.2.0-rc22.md").read_text(encoding="utf-8")
    assert "b4d6f8a0c2e4" in rc22 and "c5e7a9b1d3f4" in rc22, "rc22's notes must name its two migrations"
    versions = ROOT / "control-plane/api/alembic/versions"
    assert (versions / "b4d6f8a0c2e4_add_notification_preferences.py").exists()
    assert (versions / "c5e7a9b1d3f4_add_wiki_images.py").exists()


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


def test_the_guides_carry_no_stale_provenance_banners_and_name_moodle_and_smtp():
    """Last-mile 1.4: no 'not re-read' or lab-host banners in the role guides; 02 has a worked
    SMTP block and says Moodle is optional; 03 says what Moodle gates."""
    guides = {p.name: p.read_text(encoding="utf-8") for p in ROLE_GUIDES}
    for name, text in guides.items():
        for phrase in ("were not re-read", "not re-read end to end", "TN-MGMT01"):
            assert phrase not in text, f"{name} still carries {phrase!r}"
    install = guides["02-install-guide.md"]
    assert "tn_smtp_from_address: truenorth@" in install and "vault_smtp_password" in install
    assert "tn_moodle_enabled: true" in install
    assert "self-serve" in guides["03-admin-first-day.md"] and "tn_moodle_enabled" in guides["03-admin-first-day.md"]


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


def test_the_install_readme_carries_no_lab_specific_names():
    """install/README.md is for any site; the reference lab's names live in install/lab/
    (last-mile 1.5). Also the removed Proxmox workflows must not come back."""
    text = (ROOT / "install/README.md").read_text(encoding="utf-8")
    lab = ("TN-MGMT01", "TN-DC01", "COTE/TrueNorth-Demo", "id_ed25519_lab", "192.168.1.", "tn-staging")
    found = [w for w in lab if w in text]
    assert not found, f"lab-specific text in install/README.md (move it to install/lab/): {found}"
    for gone in (".github/workflows/terraform-plan.yml", ".github/workflows/packer-build.yml", "infra/proxmox/terraform"):
        assert not (ROOT / gone).exists(), f"{gone} was removed with the Proxmox path; do not restore it"


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


def _release_key(tag: str) -> tuple[int, int, int, int]:
    """v1.2.0-rc21 -> (1, 2, 0, 21); a final release sorts after its candidates."""
    m = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)(?:-rc(\d+))?", tag)
    assert m, f"not a release tag: {tag!r}"
    major, minor, patch, rc = m.groups()
    return int(major), int(minor), int(patch), int(rc) if rc else 10**6


def test_every_guide_carries_the_same_version_stamp_and_it_is_not_stale():
    """Last-mile 1.11: each guide opens with 'Version / Applies to / Last reviewed'. All name
    the same release; if the repository knows its tags, that release is at most two release
    candidates behind the newest (so a cut tag asks for a re-read, not on the very next PR)."""
    import subprocess

    stamp = re.compile(r"^Version: \S+ · Applies to: (v\d+\.\d+\.\d+(?:-rc\d+)?) · Last reviewed: (\d{4}-\d{2}-\d{2})$", re.M)
    found = {}
    for path in sorted((ROOT / "docs/guides").glob("*.md")):
        m = stamp.search(path.read_text(encoding="utf-8")[:600])
        assert m, f"{path.name} has no 'Version: .. · Applies to: .. · Last reviewed: ..' line near its top"
        found[path.name] = m.group(1)
    assert len(set(found.values())) == 1, f"the guides name different releases: {found}"
    applies = next(iter(found.values()))
    try:
        out = subprocess.run(
            ["git", "tag", "--list", "v*"], cwd=ROOT, capture_output=True, text=True, check=True, timeout=20
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return
    tags = [t for t in out if re.fullmatch(r"v\d+\.\d+\.\d+(?:-rc\d+)?", t)]
    if not tags:
        return  # a shallow checkout carries no tags
    newest = max(tags, key=_release_key)
    a, n = _release_key(applies), _release_key(newest)
    stale = a[:3] < n[:3] or (a[:3] == n[:3] and n[3] - a[3] > 2)
    assert not stale, f"the guides apply to {applies} but {newest} exists: re-read them and bump the stamp"
