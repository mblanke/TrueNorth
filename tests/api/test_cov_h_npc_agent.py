"""The greyspace NPC agent (app/greyspace/npc_agent.py).

Simulated people: each persona picks an action by its profile's weights and does it —
``browse`` (two real GETs: a page, then one link off it), ``resolve`` (a real DNS lookup)
or ``mail`` (real SMTP on port 25) — logging one JSON line per action and never raising
out of the loop. Everything here runs for real: browse talks to a throwaway HTTP server
on localhost, resolve to the loopback address, and the failure path (mail on a port with
no listener) is exercised through the loop itself, which must log the failure and keep
going. The sleep the loop takes is stubbed so the endless loop can be stopped after a
few actions; nothing about the agent's own logic is mocked.
"""

from __future__ import annotations

import json
import random
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, HTTPServer

from app.greyspace import npc_agent
from app.greyspace.npc_agent import Persona

PAGE = b'<html><a href="/page2.html">more</a><a href="#skip">x</a><a href="/q?a=1">q</a></html>'
UA_SEEN: list[str] = []


def _base_cfg(**overrides) -> dict:
    cfg = {
        "profile": "office",
        "personas": ["ann", "bob"],
        "sites": [{"fqdn": "intranet.example.test"}],
        "paths": ["/index.html"],
        "weights": {"browse": 0, "resolve": 0, "mail": 0},
        "mail_host": "127.0.0.1",
        "mail_domains": ["webmail.example.test"],
        "pause": [0, 0],
        "https": False,
    }
    cfg.update(overrides)
    return cfg


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        UA_SEEN.append(self.headers.get("User-Agent", ""))
        self.send_response(200)
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *args):
        pass


# ── action picking and the log line ────────────────────────────────────
def test_pick_ignores_zero_weight_actions():
    persona = Persona("ann", _base_cfg(weights={"browse": 0, "mail": 1, "resolve": 2}), 1.0, random.Random(7))
    picks = {persona.pick() for _ in range(200)}
    assert picks == {"mail", "resolve"}


def test_the_log_line_is_one_sorted_json_object_with_a_timestamp(capsys):
    npc_agent.log(persona="ann", action="browse", ok=True, site="x")
    line = capsys.readouterr().out.strip().splitlines()[0]
    fields = json.loads(line)
    assert fields["persona"] == "ann" and fields["action"] == "browse" and fields["ok"] is True
    assert fields["site"] == "x" and fields["ts"].endswith("Z")
    assert list(fields) == sorted(fields)


# ── browse against a real local site ───────────────────────────────────
def test_browse_gets_a_page_then_follows_one_same_site_link():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cfg = _base_cfg(sites=[{"fqdn": f"127.0.0.1:{port}"}])
        persona = Persona("ann", cfg, 1.0, random.Random(3))
        detail = persona.browse()
        assert detail == {
            "site": f"127.0.0.1:{port}", "path": "/index.html", "status": 200,
            "followed": "/page2.html", "scheme": "http",
        }
        assert len(UA_SEEN) >= 2  # the page and the followed link were both fetched
        assert "GreyspaceNPC/1 (ann)" in UA_SEEN[-1]  # the agent's own User-Agent
    finally:
        server.shutdown()
        thread.join(timeout=5)


# ── resolve through the real resolver ──────────────────────────────────
def test_resolve_returns_the_addresses_the_resolver_has():
    persona = Persona("ann", _base_cfg(sites=[{"fqdn": "127.0.0.1"}]), 1.0, random.Random(1))
    assert persona.resolve() == {"name": "127.0.0.1", "addresses": ["127.0.0.1"]}


# ── the loop: failures are logged, the persona keeps going ─────────────
def _run_loop_for(n_actions, cfg):
    """Drive the real run() until it has logged `n_actions` actions, then stop it."""
    real_time = time
    taken = []

    def fake_sleep(seconds):
        taken.append(seconds)
        if len(taken) > n_actions:  # the initial pause plus one pause per action
            raise KeyboardInterrupt  # BaseException: out of run(), not caught by it

    stubbed = types.SimpleNamespace(sleep=fake_sleep, strftime=real_time.strftime, gmtime=real_time.gmtime)
    persona = Persona("ann", cfg, 1000.0, random.Random(11))
    saved = npc_agent.time
    npc_agent.time = stubbed
    try:
        persona.run()
    except KeyboardInterrupt:
        pass
    finally:
        npc_agent.time = saved


def test_a_failed_action_is_logged_and_the_loop_keeps_going(capsys):
    _run_loop_for(3, _base_cfg(weights={"mail": 1}))  # nothing listens on 127.0.0.1:25

    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    actions = [entry for entry in lines if "action" in entry]
    assert len(actions) == 3
    assert all(entry["persona"] == "ann" and entry["action"] == "mail" for entry in actions)
    assert all(entry["ok"] is False for entry in actions)
    assert all(entry["error"].startswith("ConnectionRefusedError") for entry in actions)


def test_a_successful_action_is_logged_with_its_details(capsys):
    _run_loop_for(1, _base_cfg(sites=[{"fqdn": "127.0.0.1"}], weights={"resolve": 1}))

    actions = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert actions[-1] == {
        "ts": actions[-1]["ts"], "persona": "ann", "action": "resolve",
        "ok": True, "name": "127.0.0.1", "addresses": ["127.0.0.1"],
    }
