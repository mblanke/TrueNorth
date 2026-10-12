"""Docs that promise what the code does not have (R0-B, audit 2).

* docs/user-guide.md may name a UI feature only if the code has it. The "Not in v1.2.0"
  section is where absent features are listed, so it is exempt.
* Every repository path cited as evidence in docs/security.md's compliance mapping exists.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs/user-guide.md"
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
    assert not promised, f"docs/user-guide.md promises features the code lacks: {promised}"


def test_user_guide_lists_what_is_missing():
    assert "\n## Not in v1.2.0\n" in GUIDE.read_text(encoding="utf-8")


def test_security_compliance_evidence_paths_exist():
    text = SECURITY.read_text(encoding="utf-8")
    section = text[text.index("## Compliance Mapping") :]
    cited = set(re.findall(r"`((?:control-plane|infra|install|scripts|docs|tests|ai-orchestrator|\.github)/[^`\s]+)`", section))
    assert cited, "no evidence paths found in the compliance mapping"
    missing = sorted(p for p in cited if not (ROOT / p.rstrip("/")).exists())
    assert not missing, f"docs/security.md cites paths that do not exist: {missing}"
