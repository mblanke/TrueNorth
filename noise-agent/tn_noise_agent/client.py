"""Controller client: fetch the plan, report what was done. urllib only."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

TOKEN_HEADER = "X-Noise-Agent-Token"


class ControllerClient:
    """Talks to ``{base_url}/noise/agent/*``. ``base_url`` is the API root as seen from
    the management network, e.g. ``https://10.255.0.1:4200/api``."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 15.0, context=None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.context = context  # ssl.SSLContext for a private CA, if any

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method)
        req.add_header(TOKEN_HEADER, self.token)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=self.timeout, context=self.context) as resp:  # noqa: S310
            return json.loads(resp.read() or b"{}")

    def plan(self, minutes: int) -> dict:
        return self._call("GET", "/noise/agent/plan?" + urllib.parse.urlencode({"minutes": minutes}))

    def report(self, version: str, results: list[dict]) -> dict:
        return self._call("POST", "/noise/agent/report", {"version": version, "results": results})
