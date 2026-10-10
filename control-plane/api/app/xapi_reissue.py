"""Re-issue pre-switch xAPI statements under the account actor, on an operator's decision.

Statements in an LRS are immutable. The ones TrueNorth sent before migration
``5a1e9c3d7b20`` name their actor by email and their activities under
``http://truenorthrange.local/``; this tool never edits them. What it can do, when an
operator runs it with ``--apply``:

* **re-issue**: for every legacy statement of every user in ``xapi_legacy_identities``,
  store a copy under the new identity (account actor, current IRIs) whose ``id`` is
  derived from the original's (UUID v5), so a re-run stores nothing twice. The copy keeps
  the original ``timestamp`` (when it happened) and points back at the original through
  ``context.statement`` (a StatementRef) and the ``reissued-from`` extension;
* **void** (``--void``, only with ``--apply``): additionally send an ADL ``voided``
  statement for each original, so default queries return only the re-issued copy. That
  needs an LRS credential allowed to void, and it cannot be undone: a voided statement
  stays in the LRS but drops out of queries.

Without ``--apply`` it reports what it would do and sends nothing. Policy and when to run
it: docs/xapi-conformance.md, "Statements already in the LRS".

    python -m app.xapi_reissue                 # dry run: counts per user
    python -m app.xapi_reissue --apply         # store re-issued copies
    python -m app.xapi_reissue --apply --void  # and void the originals
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy.orm import Session

from . import xapi
from .lms import BaseLMSBackend, get_lms_backend
from .xapi_identity import XapiLegacyIdentity

logger = logging.getLogger("truenorth.xapi.reissue")

LEGACY_PREFIX = "http://truenorthrange.local/"
# Fixed forever: re-issued ids are uuid5(REISSUE_NAMESPACE, original id), which is what
# makes a second run a no-op. Changing it would re-issue everything a second time.
REISSUE_NAMESPACE = uuid.UUID("6f1d3c52-8a4e-5b0f-9c21-2e7a4d93b8f0")
VOIDED = "http://adlnet.gov/expapi/verbs/voided"
_SERVER_SET = ("stored", "authority", "version")  # the LRS sets these on the copy itself


def map_iri(iri: str) -> str:
    """A legacy ``http://truenorthrange.local/...`` IRI under the configured base; others unchanged."""
    if not isinstance(iri, str) or not iri.startswith(LEGACY_PREFIX):
        return iri
    rest = iri[len(LEGACY_PREFIX) :]
    head, _, tail = rest.partition("/")
    if head in ("activity-types", "extensions", "verbs"):
        return f"{xapi.iri_base()}/{head}/{tail}"
    # http://truenorthrange.local/<type>/<id>: an activity
    return xapi.activity_iri(head, tail)


def reissued_id(original_id: str) -> str:
    return str(uuid.uuid5(REISSUE_NAMESPACE, str(original_id)))


def reissue(original: dict, user_id: uuid.UUID | str) -> dict:
    """The copy of ``original`` under the new identity. Pure: no I/O."""
    new = copy.deepcopy(original)
    for key in _SERVER_SET:
        new.pop(key, None)
    new["id"] = reissued_id(original["id"])
    new["actor"] = xapi.actor(user_id)
    new["verb"]["id"] = map_iri(new["verb"]["id"])
    obj = new.get("object") or {}
    if obj.get("objectType", "Activity") == "Activity" and "id" in obj:
        obj["id"] = map_iri(obj["id"])
        definition = obj.get("definition") or {}
        if "type" in definition:
            definition["type"] = map_iri(definition["type"])
    context = new.setdefault("context", {})
    if isinstance(context.get("extensions"), dict):
        context["extensions"] = {map_iri(k): v for k, v in context["extensions"].items()}
    context.setdefault("extensions", {})[xapi.extension_iri("reissued-from")] = original["id"]
    context["statement"] = {"objectType": "StatementRef", "id": original["id"]}
    return new


def voiding(original_id: str) -> dict:
    """An ADL ``voided`` statement for ``original_id``, made by the platform itself."""
    return {
        "id": str(uuid.uuid5(REISSUE_NAMESPACE, f"void:{original_id}")),
        "actor": {
            "objectType": "Agent",
            "account": {"homePage": xapi.account_homepage(), "name": "truenorth-platform"},
        },
        "verb": {"id": VOIDED, "display": {"en": "voided"}},
        "object": {"objectType": "StatementRef", "id": original_id},
    }


def legacy_statements(backend: BaseLMSBackend, mbox: str) -> Iterator[dict]:
    """Every statement whose actor is ``mbox``, following the LRS's ``more`` links."""
    resource, params = "statements", {"agent": json.dumps({"mbox": mbox}), "ascending": "true"}
    while True:
        resp = backend.xapi_request("GET", resource, params=params)
        if resp.status != 200:
            raise RuntimeError(f"LRS answered {resp.status} to a statements query: {resp.body[:200]!r}")
        page = resp.json() or {}
        yield from page.get("statements") or []
        more = page.get("more") or ""
        if not more:
            return
        parts = urlsplit(more)
        path = parts.path
        resource = path.split("/xapi/", 1)[1] if "/xapi/" in path else path.lstrip("/")
        params = dict(parse_qsl(parts.query))


@dataclass
class Report:
    users: int = 0
    found: int = 0
    reissued: int = 0
    voided: int = 0
    skipped: int = 0  # not TrueNorth's: another authority, or no legacy TrueNorth IRI
    authorities: dict[str, int] = field(default_factory=dict)  # authority name -> legacy statements
    errors: list[str] = field(default_factory=list)


def _authority_name(stmt: dict) -> str:
    return str(((stmt.get("authority") or {}).get("account") or {}).get("name", ""))


def is_truenorth_legacy(stmt: dict) -> bool:
    """TrueNorth wrote it: its object and type are under the legacy IRI prefix. Anyone else
    able to write to the LRS could also use the old mbox; that alone proves nothing."""
    obj = stmt.get("object") or {}
    return isinstance(obj.get("id"), str) and obj["id"].startswith(LEGACY_PREFIX)


def _put(backend: BaseLMSBackend, stmt: dict) -> int:
    resp = backend.xapi_request(
        "PUT",
        "statements",
        params={"statementId": stmt["id"]},
        body=json.dumps(stmt).encode(),
        headers={"Content-Type": "application/json"},
    )
    return resp.status


def run(
    db: Session, backend: BaseLMSBackend, *, apply: bool = False, void: bool = False, authority: str | None = None
) -> Report:
    """Re-issue (and with ``void``, void) the legacy statements TrueNorth wrote: object IRIs
    under ``LEGACY_PREFIX`` and, to apply, the ``authority`` (the account name the LRS
    stamped on TrueNorth's server credential, LRS_AUTH). A dry run lists the authorities
    found, so the operator can name TrueNorth's; nothing another credential wrote is touched."""
    if void and not apply:
        raise ValueError("--void needs --apply")
    if apply and not authority:
        raise ValueError("--apply needs --authority: the LRS authority name of TrueNorth's LRS_AUTH credential")
    if not backend.supports_resources:
        raise RuntimeError(f"LMS backend {type(backend).__name__} cannot read the LRS")
    report = Report()
    for row in db.query(XapiLegacyIdentity).order_by(XapiLegacyIdentity.user_id).all():
        report.users += 1
        for original in legacy_statements(backend, row.legacy_mbox):
            if original.get("verb", {}).get("id") == VOIDED:
                continue
            if not is_truenorth_legacy(original):
                report.skipped += 1
                continue
            name = _authority_name(original)
            report.authorities[name] = report.authorities.get(name, 0) + 1
            if authority and name != authority:
                report.skipped += 1
                continue
            report.found += 1
            if not apply:
                continue
            status = _put(backend, reissue(original, row.user_id))
            if status not in (200, 204):
                report.errors.append(f"{original['id']}: re-issue answered {status}")
                continue
            report.reissued += 1
            if void:
                status = _put(backend, voiding(original["id"]))
                if status in (200, 204):
                    report.voided += 1
                else:
                    report.errors.append(f"{original['id']}: void answered {status}")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.xapi_reissue", description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="store re-issued copies (default: dry run)")
    parser.add_argument("--void", action="store_true", help="also void each original (irreversible)")
    parser.add_argument("--authority", help="LRS authority account name of TrueNorth's LRS_AUTH credential")
    args = parser.parse_args(argv)
    from .db import SessionLocal

    with SessionLocal() as db:
        report = run(db, get_lms_backend(), apply=args.apply, void=args.void, authority=args.authority)
    mode = "applied" if args.apply else "dry run"
    print(
        f"{mode}: {report.users} legacy identities, {report.found} legacy statements, "
        f"{report.reissued} re-issued, {report.voided} voided, {report.skipped} skipped, {len(report.errors)} errors"
    )
    for name, count in sorted(report.authorities.items()):
        print(f"  authority {name or '(none)'}: {count} legacy statements")
    for err in report.errors:
        print(f"  error: {err}", file=sys.stderr)
    return 1 if report.errors else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
