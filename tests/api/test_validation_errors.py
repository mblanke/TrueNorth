"""A request that fails validation is a 422, even when it carries NaN or Infinity.

Python's JSON parser accepts NaN/Infinity/-Infinity; Starlette's JSONResponse refuses to
encode them. FastAPI's default 422 echoes the bad input back, so those values used to
turn the 422 itself into a 500 (app/validation_errors.py).
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field


class _Body(BaseModel):
    x: float = Field(allow_inf_nan=False)
    n: int = 0


def _app() -> FastAPI:
    from app.validation_errors import install

    app = FastAPI()
    install(app)

    @app.post("/echo")
    def echo(body: _Body) -> dict:
        return {"x": body.x}

    return app


@pytest.fixture
def bare():
    return TestClient(_app(), raise_server_exceptions=True)


@pytest.mark.parametrize(("literal", "spelled"), [("NaN", "NaN"), ("Infinity", "Infinity"), ("-Infinity", "-Infinity")])
def test_non_finite_input_is_a_422_that_names_it(bare, literal, spelled):
    r = bare.post("/echo", content=f'{{"x": {literal}}}', headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    (err,) = r.json()["detail"]
    assert err["loc"] == ["body", "x"]
    assert err["input"] == spelled


def test_non_finite_nested_inside_a_rejected_body_is_spelled_out(bare):
    """The whole body is echoed when it is the wrong type; any NaN inside must still encode."""
    r = bare.post("/echo", content='[1.5, NaN, {"y": Infinity}]', headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert r.json()["detail"][0]["input"] == [1.5, "NaN", {"y": "Infinity"}]


def test_ordinary_validation_errors_keep_fastapis_shape(bare):
    r = bare.post("/echo", json={"x": "not a number", "n": "nope"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert {tuple(e["loc"]) for e in detail} == {("body", "x"), ("body", "n")}
    assert all({"type", "loc", "msg", "input"} <= set(e) for e in detail)


def test_the_real_app_returns_422_for_nan(client):
    """Through the full middleware stack, on a real float field (the board position)."""
    t = client.post("/tickets", json={"subject": "x"}).json()
    r = client.post(
        f"/tickets/{t['id']}/move",
        content='{"status": "open", "board_order": NaN}',
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422
    assert r.json()["detail"][0]["input"] == "NaN"
