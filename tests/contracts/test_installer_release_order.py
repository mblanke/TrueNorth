"""The installer orders release tags with rcN numerically (install/roles/tn_app_source).

Work install, 2026-10-20: `is version(..., version_type='semver')` judged v1.2.0-rc12
older than v1.2.0-rc9 (SemVer compares "rc12" and "rc9" as strings) and refused the
upgrade. These tests pin the filter that replaced it and evaluate the role's real assert.
"""

from __future__ import annotations

import functools
import importlib.util
import itertools
from pathlib import Path

import jinja2
import pytest
import yaml

ROLE = Path(__file__).resolve().parents[2] / "install/roles/tn_app_source"


def _load_plugin():
    spec = importlib.util.spec_from_file_location("tn_release", ROLE / "filter_plugins/tn_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin()
cmp = plugin.tn_release_cmp


def test_rc_numbers_compare_numerically_and_final_follows_its_rcs():
    ordered = [
        "v1.2.0-rc1",
        "v1.2.0-rc9",
        "v1.2.0-rc10",
        "v1.2.0-rc11",
        "v1.2.0-rc12",
        "v1.2.0",
        "v1.2.1-rc1",
        "v1.2.1",
        "v1.10.0-rc2",
        "v1.10.0",
        "v2.0.0-rc1",
    ]
    shuffled = list(reversed(ordered))
    assert sorted(shuffled, key=functools.cmp_to_key(cmp)) == ordered
    for older, newer in itertools.pairwise(ordered):
        assert cmp(older, newer) == -1, (older, newer)
        assert cmp(newer, older) == 1, (newer, older)


@pytest.mark.parametrize(
    "a, b", [("v1.2.0-rc12", "v1.2.0-rc12"), ("1.2.0-rc12", "v1.2.0-rc12"), ("v1.2.0", "v1.2.0+build.7")]
)
def test_equal_tags(a, b):
    assert cmp(a, b) == 0


def test_other_prerelease_shapes_fall_back_to_semver():
    # SemVer 2.0 section 11 example, in order.
    ordered = [
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
    ]
    for older, newer in itertools.pairwise(ordered):
        assert cmp(older, newer) == -1, (older, newer)
    assert cmp("v1.0.0-beta2", "v1.0.0-beta10") == -1
    assert cmp("v1.0.0-rc9", "v1.0.0-beta10") == 1  # different letters: ASCII, as SemVer


@pytest.mark.parametrize("bad", ["", "latest", "v1.2", "v1.2.0-", "v01.2.0"])
def test_a_malformed_tag_is_an_error_not_a_silent_order(bad):
    with pytest.raises(ValueError):
        cmp(bad, "v1.2.0")


def test_filter_is_exported():
    assert plugin.FilterModule().filters() == {"tn_release_cmp": cmp}


# ── The role's real assert, evaluated as Ansible would ─────────────────
def _refuse_older_release_expression() -> str:
    tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
    (task,) = [t for t in tasks if t.get("name") == "App source — refuse an older release"]
    that = task["ansible.builtin.assert"]["that"]
    assert "version_type" not in that  # the SemVer string compare must not come back
    return that


def _allowed(new: str, prev_state: dict, allow_downgrade: bool = False) -> bool:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in {"1", "true", "yes", "on"}
    env.filters.update(plugin.FilterModule().filters())
    expr = env.compile_expression(_refuse_older_release_expression(), undefined_to_none=False)
    return bool(expr(tn_release_version=new, tn_app_prev_state=prev_state, tn_allow_downgrade=allow_downgrade))


def test_upgrade_from_rc9_to_rc12_is_allowed():
    assert _allowed("v1.2.0-rc12", {"release": "v1.2.0-rc9"})
    assert _allowed("v1.2.0-rc10", {"release": "v1.2.0-rc9"})
    assert _allowed("v1.2.0", {"release": "v1.2.0-rc12"})
    assert _allowed("v1.2.1-rc1", {"release": "v1.2.0"})


def test_reinstalling_the_same_release_is_allowed():
    assert _allowed("v1.2.0-rc12", {"release": "v1.2.0-rc12"})


def test_downgrade_is_still_refused_unless_asked_for():
    assert not _allowed("v1.2.0-rc11", {"release": "v1.2.0-rc12"})
    assert not _allowed("v1.2.0-rc9", {"release": "v1.2.0-rc12"})
    assert not _allowed("v1.2.0-rc12", {"release": "v1.2.0"})
    assert _allowed("v1.2.0-rc11", {"release": "v1.2.0-rc12"}, allow_downgrade=True)


def test_first_install_has_nothing_to_compare():
    assert _allowed("v1.2.0-rc12", {})
    assert _allowed("v1.2.0-rc12", {"release": ""})
