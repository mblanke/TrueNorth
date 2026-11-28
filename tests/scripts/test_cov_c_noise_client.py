# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""tn_noise_agent.client.ControllerClient (kept under tests/scripts/ as this card's owned
test location): plan() GETs /noise/agent/plan with the token header and reports() POSTs the
results as JSON — against a real localhost HTTP server, so the wire format is what is
asserted. An empty response body must come back as {}."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from tn_noise_agent.client import TOKEN_HEADER, ControllerClient


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence the test run
        pass

    def _log(self):
        length = int(self.headers.get("Content-Length") or 0)
        seen.append(
            {
                "method": self.command,
                "path": self.path,
                "token": self.headers.get(TOKEN_HEADER),
                "accept": self.headers.get("Accept"),
                "content_type": self.headers.get("Content-Type"),
                "body": self.rfile.read(length) if length else b"",
            }
        )

    def _reply(self, body: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._log()
        payload = b"" if empty[0] else json.dumps({"minutes": 30, "tasks": ["phish"]}).encode()
        self._reply(payload)

    def do_POST(self):
        self._log()
        self._reply(b"" if empty[0] else json.dumps({"received": True}).encode())


seen: list[dict] = []
empty = [False]


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_plan_gets_the_plan_with_the_token_header(server):
    client = ControllerClient(server + "/", "tok123")  # trailing slash must be stripped
    assert client.plan(30) == {"minutes": 30, "tasks": ["phish"]}
    req = seen[-1]
    assert req["method"] == "GET"
    assert req["path"] == "/noise/agent/plan?minutes=30"
    assert req["token"] == "tok123"
    assert req["accept"] == "application/json"
    assert req["content_type"] is None  # GETs carry no body


def test_report_posts_the_results_as_json(server):
    client = ControllerClient(server, "tok123")
    assert client.report("v1", [{"kind": "dns"}]) == {"received": True}
    req = seen[-1]
    assert req["method"] == "POST"
    assert req["path"] == "/noise/agent/report"
    assert req["token"] == "tok123"
    assert req["content_type"] == "application/json"
    assert json.loads(req["body"]) == {"version": "v1", "results": [{"kind": "dns"}]}


def test_an_empty_response_body_comes_back_as_an_empty_dict(server):
    client = ControllerClient(server, "tok123")
    empty[0] = True
    try:
        assert client.plan(5) == {}
        assert client.report("v1", []) == {}
    finally:
        empty[0] = False
