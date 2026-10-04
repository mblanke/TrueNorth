"""PUT/PATCH bodies must refuse an explicit null for a NOT NULL column.

Update handlers apply ``model_dump(exclude_unset=True)`` with ``setattr``, so a field
sent as ``null`` is written as NULL. On a NOT NULL column that is an IntegrityError at
commit: a 500 instead of a 422. The convention is ``x: T = Field(default=None)``:
omitting the field keeps the stored value, ``null`` fails validation.

The body -> table pairing is inferred: the ORM classes named in the handler's source.
"""

from __future__ import annotations

import inspect
import re
import typing

import pytest
from app import models
from app.main import app
from fastapi.routing import APIRoute, iter_route_contexts
from pydantic import BaseModel, ValidationError

# Handlers that skip None values themselves, so a null is ignored rather than written.
IGNORES_NULL = {
    ("PATCH", "/golden-images/{image_id}"),
    ("PATCH", "/learning-paths/{lp_id}"),
}

_TABLES = {m.class_.__name__: m.class_ for m in models.Base.registry.mappers}


def _cases():
    # FastAPI >= 0.13x keeps included routers behind one wrapper in app.routes; walk the
    # effective routes (prefixed paths, merged dependencies) instead.
    for route in iter_route_contexts(app.routes):
        if not isinstance(route.original_route, APIRoute):
            continue
        for method in sorted({"PUT", "PATCH"} & route.methods):
            if (method, route.path) in IGNORES_NULL:
                continue
            bodies = [bp.field_info.annotation for bp in route.dependant.body_params]
            body = next((b for b in bodies if inspect.isclass(b) and issubclass(b, BaseModel)), None)
            if body is None:
                continue
            src = inspect.getsource(route.endpoint)
            tables = [cls for name, cls in _TABLES.items() if re.search(rf"\b{name}\b", src)]
            for field in body.model_fields:
                for table in tables:
                    col = table.__table__.columns.get(field)
                    if col is not None and not col.nullable:
                        yield pytest.param(body, field, id=f"{method} {route.path} {body.__name__}.{field}")
                        break


CASES = list(_cases())


def test_sweep_finds_the_update_bodies():
    # Guard the guard: if introspection silently found nothing, every case below passes.
    assert len(CASES) > 50


@pytest.mark.parametrize(("body", "field"), CASES)
def test_null_is_refused_for_not_null_column(body: type[BaseModel], field: str):
    annotation = body.model_fields[field].annotation
    assert type(None) not in typing.get_args(annotation), (
        f"{body.__name__}.{field} accepts null but its column is NOT NULL; "
        f"declare it `{field}: T = Field(default=None)`"
    )
    with pytest.raises(ValidationError):
        body.model_validate({field: None})


def test_omitting_an_optional_field_still_means_keep_it():
    # Full-replacement bodies (every field required) have nothing to omit.
    partial = {c.values[0] for c in CASES if not any(f.is_required() for f in c.values[0].model_fields.values())}
    assert partial
    for body in partial:
        assert body.model_validate({}).model_dump(exclude_unset=True) == {}
