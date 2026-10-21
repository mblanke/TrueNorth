"""The ADR 0013 §3 data migration (Alembic e0f1a2b3c4d5): existing ranges keep working,
nothing holds a credential it should not.

* **Reseal** every stored range password from ``tnsec:v1:`` (the general secretbox) to
  ``tnsec:rc1:`` (the range-credential key, ``seal.py``). ``downgrade`` reverses it.
* **Implicit staff logins** for every existing range (``accounts.implicit_staff_logins``):
  generated, sealed, ``applied = false`` and noted "template password still valid until
  rebuilt": the range's VMs were built before, so they still have the template password
  until the range is rebuilt. A lab session's go to its Student, as new labs' do.
* **Wiki**: every generated range page is rendered again without credentials (``wiki_doc``
  version 2) as a new revision, and ADR 0012's ``{{tn-credential:...}}`` tokens are
  stripped from the pages' earlier revisions. Wiki search reads the page bodies (SQL), so
  there is no separate index to rebuild.

Everything here is Core SQL on frozen column lists, never the ORM: a later model change
must not break this migration on an older database. Needs ``TN_SECRETS_KEY`` for the
sealing steps; without it they are skipped with a warning (``RangeSealedText`` still
reads ``v1`` rows, and the implicit rows are made by the next build of each range).
"""

from __future__ import annotations

import json
import logging
import types
import uuid

import sqlalchemy as sa

from .. import safe_yaml, secretbox
from ..models import GUID
from . import accounts as account_rules
from . import seal as rc_seal
from . import wiki_doc

logger = logging.getLogger("alembic.range_credentials")

SYSTEM_ACTOR = uuid.UUID("00000000-0000-0000-0000-00000000c0de")  # service.SYSTEM_ACTOR
NOT_HARDENED = ("template password still valid until rebuilt: this range was built before ADR 0013, so the "
                "machine keeps its golden template's password until the range is rebuilt")

_rc = sa.table(
    "range_credentials",
    sa.column("id", GUID()), sa.column("tenant_id", GUID()), sa.column("range_id", GUID()),
    sa.column("machine", sa.String), sa.column("username", sa.String), sa.column("password", sa.Text),
    sa.column("generated", sa.Boolean), sa.column("student_visible", sa.Boolean), sa.column("applied", sa.Boolean),
    sa.column("role", sa.String), sa.column("notes", sa.Text), sa.column("source", sa.String),
    sa.column("scenario", sa.Boolean),
)
_ranges = sa.table(
    "ranges",
    sa.column("id", GUID()), sa.column("tenant_id", GUID()), sa.column("template_id", GUID()),
    sa.column("name", sa.String), sa.column("state", sa.String), sa.column("provisioner_backend", sa.String),
    sa.column("provisioner_output", sa.Text), sa.column("deleted_at", sa.DateTime),
)
_templates = sa.table("templates", sa.column("id", GUID()), sa.column("tenant_id", GUID()),
                      sa.column("name", sa.String), sa.column("version", sa.String), sa.column("yaml", sa.Text))
_labs = sa.table("lab_sessions", sa.column("range_id", GUID()), sa.column("retired_ranges", sa.Text))
_pages = sa.table(
    "wiki_pages",
    sa.column("id", GUID()), sa.column("tenant_id", GUID()), sa.column("title", sa.String),
    sa.column("body", sa.Text), sa.column("tags", sa.String), sa.column("revision_number", sa.Integer),
    sa.column("last_editor_id", GUID()), sa.column("deleted_at", sa.DateTime),
)
_revisions = sa.table(
    "wiki_revisions",
    sa.column("id", GUID()), sa.column("tenant_id", GUID()), sa.column("page_id", GUID()),
    sa.column("revision_number", sa.Integer), sa.column("title", sa.String), sa.column("body", sa.Text),
    sa.column("editor_id", GUID()), sa.column("edit_summary", sa.String),
)


def key_available() -> bool:
    try:
        rc_seal.seal("probe")
    except secretbox.SecretKeyMissingError:
        return False
    return True


# ── Passwords ───────────────────────────────────────────────────────────
def reseal(conn, *, to: str = "rc1") -> int:
    """Seal every stored password with the range-credential key (``to="rc1"``) or back with
    the general secretbox (``to="v1"``, for the downgrade). Returns rows changed."""
    changed = 0
    for row_id, stored in conn.execute(sa.select(_rc.c.id, _rc.c.password)).all():
        if not stored:
            continue
        if to == "rc1" and not stored.startswith(rc_seal.PREFIX):
            new = rc_seal.seal(secretbox.unseal(stored) or "")
        elif to == "v1" and stored.startswith(rc_seal.PREFIX):
            new = secretbox.seal(rc_seal.unseal(stored))
        else:
            continue
        conn.execute(sa.update(_rc).where(_rc.c.id == row_id).values(password=new))
        changed += 1
    return changed


def _lab_ranges(conn) -> set[str]:
    out: set[str] = set()
    for range_id, retired in conn.execute(sa.select(_labs.c.range_id, _labs.c.retired_ranges)).all():
        if range_id is not None:
            out.add(str(range_id))
        try:
            out.update(str(r) for r in json.loads(retired or "[]"))
        except ValueError:
            continue
    return out


def _doc(text: str | None) -> dict:
    try:
        doc = safe_yaml.load(text or "")
    except Exception:  # noqa: BLE001 — an unreadable template builds no machine
        return {}
    return doc if isinstance(doc, dict) else {}


def add_implicit_logins(conn) -> int:
    """An implicit staff login row per machine of every existing range that has none yet.
    Returns rows added."""
    labs = _lab_ranges(conn)
    added = 0
    stmt = (sa.select(_ranges.c.id, _ranges.c.tenant_id, _templates.c.yaml)
            .select_from(_ranges.outerjoin(_templates, _templates.c.id == _ranges.c.template_id))
            .where(_ranges.c.deleted_at.is_(None), _ranges.c.tenant_id.isnot(None)))
    for range_id, tenant_id, text in conn.execute(stmt).all():
        have = conn.execute(sa.select(_rc.c.machine, _rc.c.username).where(_rc.c.range_id == range_id)).all()
        existing = [account_rules.AccountSpec(machine=m, username=u, password=None, generate=True,
                                              student_visible=False, role="admin", notes="") for m, u in have]
        for spec in account_rules.implicit_staff_logins(_doc(text), existing,
                                                        student_visible=str(range_id) in labs):
            conn.execute(sa.insert(_rc).values(
                id=uuid.uuid4(), tenant_id=tenant_id, range_id=range_id, machine=spec.machine,
                username=spec.username, password=rc_seal.seal(account_rules.generate_password(spec.username)),
                generated=True, student_visible=spec.student_visible, applied=False, role="admin",
                notes=f"{spec.notes}; {NOT_HARDENED}"[:500], source="implicit", scenario=False))
            added += 1
    return added


# ── Wiki ────────────────────────────────────────────────────────────────
def _range_of(tags: str | None) -> str | None:
    for tag in (tags or "").split(","):
        if tag.startswith("range:"):
            return tag[6:]
    return None


def regenerate_pages(conn, *, with_rows: bool = True) -> int:
    """Render every generated range page again (``wiki_doc`` version 2, no credentials), as
    a new revision. A page whose range is gone keeps its body, tokens stripped. Returns
    pages rewritten."""
    pages = conn.execute(sa.select(_pages).where(_pages.c.deleted_at.is_(None),
                                                 _pages.c.tags.like(f"%{wiki_doc.GENERATED_TAG}%"))).all()
    changed = 0
    for page in pages:
        if wiki_doc.GENERATED_TAG not in (page.tags or "").split(","):
            continue
        body = _regenerated(conn, page, with_rows) or wiki_doc.strip_legacy_tokens(page.body)
        if body == page.body:
            continue
        number = (page.revision_number or 0) + 1
        conn.execute(sa.update(_pages).where(_pages.c.id == page.id).values(
            body=body, revision_number=number, last_editor_id=SYSTEM_ACTOR))
        conn.execute(sa.insert(_revisions).values(
            id=uuid.uuid4(), tenant_id=page.tenant_id, page_id=page.id, revision_number=number, title=page.title,
            body=body, editor_id=SYSTEM_ACTOR, edit_summary="Regenerated by TrueNorth without credentials (ADR 0013)"))
        changed += 1
    return changed


def _regenerated(conn, page, with_rows: bool) -> str | None:
    rid = _range_of(page.tags)
    try:
        range_id = uuid.UUID(rid) if rid else None
    except ValueError:
        return None
    if range_id is None:
        return None
    rng = conn.execute(sa.select(_ranges).where(_ranges.c.id == range_id, _ranges.c.deleted_at.is_(None))).first()
    if rng is None:
        return None
    tmpl = conn.execute(sa.select(_templates).where(_templates.c.id == rng.template_id)).first()
    doc = _doc(tmpl.yaml if tmpl else "")
    doc["_tn_template"] = {"name": tmpl.name if tmpl else "", "version": tmpl.version if tmpl else ""}
    cols = [_rc.c.id, _rc.c.machine, _rc.c.username, _rc.c.role, _rc.c.generated, _rc.c.student_visible,
            _rc.c.applied, _rc.c.notes]
    if with_rows:
        cols += [_rc.c.source, _rc.c.scenario]
    rows = [types.SimpleNamespace(**{"source": "declared", "scenario": False, **r._mapping})
            for r in conn.execute(sa.select(*cols).where(_rc.c.range_id == range_id)
                                  .order_by(_rc.c.machine, _rc.c.username)).all()]
    view = types.SimpleNamespace(id=rng.id, name=rng.name, state=rng.state,
                                 provisioner_backend=rng.provisioner_backend,
                                 provisioner_output=rng.provisioner_output)
    notes = wiki_doc.strip_legacy_tokens(wiki_doc.notes_of(page.body or ""))
    return wiki_doc.compose(wiki_doc.render(view, rows, doc), notes)


def strip_revision_tokens(conn) -> int:
    """ADR 0012's credential tokens out of every earlier wiki revision. Returns revisions changed."""
    stmt = sa.select(_revisions.c.id, _revisions.c.body).where(_revisions.c.body.like("%{{tn-credential:%"))
    changed = 0
    for rev_id, body in conn.execute(stmt).all():
        clean = wiki_doc.strip_legacy_tokens(body)
        if clean != body:
            conn.execute(sa.update(_revisions).where(_revisions.c.id == rev_id).values(body=clean))
            changed += 1
    return changed


# ── Entry points (the Alembic revision) ─────────────────────────────────
def upgrade(conn) -> dict[str, int]:
    done = {"resealed": 0, "implicit_logins": 0}
    if key_available():
        done["resealed"] = reseal(conn, to="rc1")
        done["implicit_logins"] = add_implicit_logins(conn)
    else:
        logger.warning("TN_SECRETS_KEY is not set: range passwords stay tnsec:v1 until next written, and existing "
                       "ranges get their implicit staff logins at their next build")
    done["pages"] = regenerate_pages(conn)
    done["revisions"] = strip_revision_tokens(conn)
    logger.info("ADR 0013 credentials migration: %s", done)
    return done


def downgrade(conn) -> int:
    """Back to ``tnsec:v1`` for the code before ADR 0013 (it reads nothing else)."""
    if not key_available():
        logger.warning("TN_SECRETS_KEY is not set: tnsec:rc1 range passwords cannot be resealed for the old code")
        return 0
    return reseal(conn, to="v1")
