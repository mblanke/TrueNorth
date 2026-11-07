"""Course lab steps only name hosts the range template actually builds.

Every course module may carry a ``lab_range`` naming a template under
``content/ranges/`` and steps whose ``hosts`` list names nodes from that
template. If a step names a host the template does not define, the lab
checklist for the course cannot be walked and provisioning has nothing to
start for it; these checks keep the course corpus and the templates in lockstep.

Two checks:

* every host named in every ``lab_range`` step of every course file exists as a
  node ``id`` in that course's declared template (the seven held courses --
  C302, C305, C306, RMC C202, RMC C205, RMC C207, RMC C211 -- carry no
  ``lab_range`` yet, so they are out of scope by construction, not by list);
* every range template has a lab checklist under ``docs/lab-checks/``. The
  checklists land in sibling commits of card #478, so that check is
  ``xfail(strict=False)`` until they arrive -- it reports xfail, not pass,
  while the directories exist but the docs do not. Remove the marker once the
  eight checklists are committed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COURSE_DIR = ROOT / "content/courses"
RANGE_DIR = ROOT / "content/ranges"
LAB_CHECKS = ROOT / "docs/lab-checks"
COURSE_FILES = sorted(COURSE_DIR.glob("*.yaml"))

# The eight range templates with course material (content/ranges/ holds only
# these directories). Card #478: docs/lab-checks/<name>.md for each.
TEMPLATES = [
    "cloud-security",
    "greyspace-internet",
    "large-enterprise",
    "medium-enterprise",
    "red-team",
    "red-vs-blue",
    "small-enterprise",
    "soc-training",
]


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _lab_steps(doc: dict):
    """(module ordinal, template, step) for every lab_range step in a course."""
    for module in doc.get("modules") or []:
        lab = module.get("lab_range") or {}
        template = lab.get("template")
        if not template:
            continue
        for step in lab.get("steps") or []:
            yield module.get("ordinal"), template, step


@pytest.mark.parametrize("path", COURSE_FILES, ids=lambda p: p.name)
def test_lab_step_hosts_exist_in_the_template(path):
    """Every host a lab step names is a node the template builds."""
    doc = _load(path)
    missing = []
    node_ids: dict[str, set[str]] = {}
    for ordinal, template, step in _lab_steps(doc):
        if template not in node_ids:
            template_yaml = RANGE_DIR / template / "template.yaml"
            assert template_yaml.exists(), f"{path.name}: unknown template {template}"
            node_ids[template] = {n["id"] for n in _load(template_yaml).get("nodes") or []}
        for host in step.get("hosts") or []:
            if host not in node_ids[template]:
                missing.append(f"module {ordinal}: {host} not in {template}")
    assert not missing, f"{path.name}:\n  " + "\n  ".join(missing)


@pytest.mark.xfail(strict=False, reason="checklists land in sibling commits of card #478")
@pytest.mark.parametrize("name", TEMPLATES)
def test_every_template_has_a_lab_checklist(name):
    assert (RANGE_DIR / name).is_dir(), f"no range template {name}"
    assert (LAB_CHECKS / f"{name}.md").is_file(), f"no docs/lab-checks/{name}.md"
