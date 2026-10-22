"""The record's sections and the verdict (ADR 0013 §6). Pure functions, no database.

A section's state is its **latest** entry (highest ``seq``); a correction is just a later
entry. No entry at all is ``missing``. Entry statuses:

=============  ==============================================================
``pass``       the evidence is complete and the check passed
``fail``       a check failed outright (a reachable zone outside its
               exceptions, a template password that still works, a pinned
               build mismatch, a VM whose guest network never came up ...)
``incomplete`` some evidence is there, but less than certification needs
               (e.g. template names without promoted build ids until S2)
``info``       recorded for the trail, judged by nobody (``overrides``)
=============  ==============================================================

Verdict, over every section:

* any section ``fail``  -> ``not_safe`` ("Not safe"), the failures listed;
* else any **required** section ``missing`` or ``incomplete`` -> ``ready_not_certified``
  ("Ready, not certified"), each reason listed;
* else ``certified`` ("Certified for Students").

Optional sections (drift, teardown) cannot hold a record back by being absent, but a
failure in them is still ``not_safe``. ``overrides`` never changes the verdict.
"""

from __future__ import annotations

from dataclasses import dataclass

PASS, FAIL, INCOMPLETE, INFO, MISSING = "pass", "fail", "incomplete", "info", "missing"
STATUSES = (PASS, FAIL, INCOMPLETE, INFO)  # what an entry may say; MISSING is computed

CERTIFIED, READY_NOT_CERTIFIED, NOT_SAFE = "certified", "ready_not_certified", "not_safe"
VERDICT_LABELS = {
    CERTIFIED: "Certified for Students",
    READY_NOT_CERTIFIED: "Ready, not certified",
    NOT_SAFE: "Not safe",
}


@dataclass(frozen=True)
class Section:
    name: str
    title: str
    required: bool
    provided_by: str  # the slice or issue that writes it


SECTIONS: tuple[Section, ...] = (
    Section("template_provenance", "Template provenance", True, "S2 (#240)"),
    Section("network_allocation", "Network allocation", True, "S1 (#239)"),
    Section("guest_readiness", "Guest readiness", True, "#232"),
    Section("credentials", "Credential state", True, "S3 (#241)"),
    Section("isolation", "Isolation", True, "S4 (#242)"),
    Section("drift", "Drift", False, "S5 (#243)"),
    Section("teardown", "Teardown", False, "S5 (#243)"),
    Section("overrides", "Approvals, overrides and forces", False, "every slice"),
)
BY_NAME = {s.name: s for s in SECTIONS}
INFO_ONLY = frozenset({"overrides"})


def latest(entries: list, section: str):
    """The entry that decides ``section``'s state, or None. ``entries``: objects with
    ``section``, ``seq``, ``status``, ``summary``."""
    mine = [e for e in entries if e.section == section]
    return max(mine, key=lambda e: e.seq) if mine else None


def section_status(entries: list, section: str) -> str:
    if section in INFO_ONLY:
        return INFO
    top = latest(entries, section)
    return top.status if top is not None else MISSING


def verdict(entries: list) -> tuple[str, list[str]]:
    """(verdict, reasons). Reasons name the section and say what is wrong or missing."""
    failures, gaps = [], []
    for s in SECTIONS:
        if s.name in INFO_ONLY:
            continue
        top = latest(entries, s.name)
        if top is not None and top.status == FAIL:
            failures.append(f"{s.title} failed: {top.summary}".rstrip(": "))
        elif s.required and top is None:
            gaps.append(f"{s.title} evidence missing (provided by {s.provided_by})")
        elif s.required and top.status == INCOMPLETE:
            gaps.append(f"{s.title} incomplete: {top.summary}".rstrip(": "))
    if failures:
        return NOT_SAFE, failures
    if gaps:
        return READY_NOT_CERTIFIED, gaps
    return CERTIFIED, []
