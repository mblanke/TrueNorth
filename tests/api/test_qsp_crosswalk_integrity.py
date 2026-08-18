"""Integrity checks on the QSP crosswalk — the accreditation contract.

crosswalk.csv is what an auditor reads to see that each performance objective traces
to a recognised workforce framework. Two things must stay true: the mappings are
actually present, and the framework version claimed is the one actually used.
"""

import csv
import pathlib
import re

CROSSWALK = pathlib.Path(__file__).resolve().parents[2] / \
    "truenorth-content-pack/truenorth-content/crosswalk.csv"

# Rows legitimately without a framework mapping, with the reason.
UNMAPPED_OK = {
    ("TEMP67", "PO_TODO"): "status=needs_spec placeholder for the remaining Cpl POs",
}


def _rows():
    with open(CROSSWALK, newline="") as f:
        return list(csv.DictReader(f))


def test_every_specified_po_has_a_framework_mapping():
    """No row should still say TODO-map except the documented placeholder."""
    unmapped = [
        (r["qsp_code"], r["po_id"])
        for r in _rows()
        if r["nice_dcwf_task"].startswith("TODO-map")
    ]
    assert set(unmapped) <= set(UNMAPPED_OK), (
        f"unmapped rows that are not documented placeholders: "
        f"{set(unmapped) - set(UNMAPPED_OK)}"
    )


def test_mappings_carry_a_work_role_and_at_least_one_task():
    """`ROLE:Txxxx;Txxxx` — a role with no tasks is not a usable mapping."""
    for r in _rows():
        v = r["nice_dcwf_task"]
        if v.startswith("TODO-map") or v in ("", "-", "n/a"):
            continue
        role, _, tasks = v.partition(":")
        assert re.fullmatch(r"[A-Z]{2}-[A-Z]{3}-\d{3}", role), \
            f"{r['po_id']}: bad work-role id {role!r}"
        ids = [t for t in tasks.split(";") if t and t != "DCWF-TODO"]
        assert ids, f"{r['po_id']}: work role with no task ids"
        for t in ids:
            assert re.fullmatch(r"T\d{4}", t), f"{r['po_id']}: bad task id {t!r}"


def test_component_version_matches_the_ids_actually_used():
    """Do not claim a framework version the identifiers do not come from.

    The task ids are T0xxx and the work roles are PR-CDA-001 style — both are
    NIST SP 800-181 rev 1. Claiming NICE Framework Components v2.1.0 while using
    rev-1 identifiers is the kind of discrepancy an accreditation review catches.
    """
    for r in _rows():
        assert r["component_version"] == "SP800-181r1", (
            f"{r['po_id']}: component_version={r['component_version']!r} but the ids "
            f"in use are SP 800-181 rev 1"
        )


def test_dcwf_gaps_stay_visible():
    """Red Analyst rows asked for DCWF codes; only NICE was available.

    They must keep the DCWF-TODO marker rather than reading as fully mapped.
    """
    red = [r for r in _rows() if r["qsp_code"] == "TEMP64"]
    assert red, "expected TEMP64 (Red Analyst) rows"
    for r in red:
        assert "DCWF-TODO" in r["nice_dcwf_task"], (
            f"{r['po_id']}: DCWF mapping is still outstanding and must stay flagged"
        )
