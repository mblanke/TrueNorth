"""Greyspace NPC traffic: simulated users of the simulated internet (plan slice 6, ADR 0007).

A lightweight equivalent of GHOSTS, chosen over it for the stack (docs/greyspace-plan.md,
slice 6): GHOSTS needs a .NET client on every endpoint plus its API server and Postgres,
while this runs as one small container inside the Greyspace stack and is exercised by the
CI T0 check. GHOSTS clients in the range's workstation images remain a later option;
the profiles below use the same vocabulary (personas, timelines, activity weights).

``npc_agent.py`` (next to this file) is the agent: standard-library Python, run in the
pinned ``python`` image. ``agent_config(profile, sites, ...)`` is what it reads: the
personas, the sites they visit (the block's selected sites), the mail host, and how busy
they are. Every request carries ``User-Agent: ... GreyspaceNPC/1 (<persona>)`` so the
web farm's access log separates NPC traffic from Students'.

Pure standard library.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

IMAGE = "python:3.12-alpine3.20@sha256:25849f9599e06dfe4d11b552e06f5ac4cc2ad342054eb81f7877e611f6f87c66"

# name -> profile. ``pause`` is the seconds between one persona's actions (min, max),
# divided by GS_NPC_SPEED at run time; ``weights`` how often each action is picked.
PROFILES: dict[str, dict[str, Any]] = {
    "office-day": {
        "title": "Office day",
        "description": "A dozen staff browsing news, search, social and dev sites, resolving names and sending mail.",
        "personas": [
            "a.martin", "b.singh", "c.tremblay", "d.nguyen", "e.roy", "f.cote",
            "g.wilson", "h.leblanc", "i.chen", "j.gagnon", "k.brown", "l.morin",
        ],
        "pause": [4.0, 20.0],
        "weights": {"browse": 6, "resolve": 2, "mail": 1},
        "categories": ["news", "search", "social", "webmail", "dev", "shopping", "gov", "video"],
    },
    "quiet-night": {
        "title": "Quiet night",
        "description": "Two night-shift users and background lookups: sparse, mostly news and DNS.",
        "personas": ["night.ops1", "night.ops2"],
        "pause": [30.0, 120.0],
        "weights": {"browse": 2, "resolve": 3, "mail": 0},
        "categories": ["news", "search"],
    },
}
PROFILE_NAMES = ("off", *PROFILES)


def agent_source() -> str:
    """The agent script the stack mounts as ``npc/npc.py``."""
    return Path(__file__).with_name("npc_agent.py").read_text(encoding="utf-8")


def agent_config(profile: str, sites: list, *, mail_host: str, https: bool) -> dict[str, Any]:
    """The agent's ``config.json`` for ``profile`` over the block's selected ``sites``."""
    spec = PROFILES[profile]
    wanted = set(spec["categories"])
    chosen = [s for s in sites if s.category in wanted] or list(sites)
    mail_domains = sorted({s.fqdn for s in sites if s.category == "webmail"} or {s.fqdn for s in chosen[:3]})
    return {
        "format": "greyspace-npc/1",
        "profile": profile,
        "personas": list(spec["personas"]),
        "pause": list(spec["pause"]),
        "weights": dict(spec["weights"]),
        "sites": [{"fqdn": s.fqdn, "category": s.category} for s in chosen],
        "paths": ["/", "/about.html", "/news/latest.html"],
        "mail_host": mail_host,
        "mail_domains": mail_domains,
        "https": https,
        "ca_file": "/certs/root.crt" if https else "",
    }
