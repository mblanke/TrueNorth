"""TrueNorth Range - 422 responses that can always be encoded.

FastAPI's default handler for a request that fails validation echoes the offending
input back (``detail[].input``). Python's JSON parser accepts ``NaN``, ``Infinity`` and
``-Infinity``, but Starlette's ``JSONResponse`` refuses to encode them
(``allow_nan=False``). So a body such as ``{"board_order": NaN}`` turned the 422 into
an unhandled ``ValueError``: a 500, for every float field in the API.

This handler returns the same shape as FastAPI's (``{"detail": [...]}``, status 422),
with any non-finite float in the error list written as the string JSON would have
needed: ``"NaN"``, ``"Infinity"`` or ``"-Infinity"``.
"""

from __future__ import annotations

import math
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


def _finite(value: Any) -> Any:
    """`value` with every non-finite float replaced by its JSON-literal spelling."""
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_finite(v) for v in value]
    return value


async def request_validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": _finite(jsonable_encoder(exc.errors()))})


def install(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, request_validation_handler)
