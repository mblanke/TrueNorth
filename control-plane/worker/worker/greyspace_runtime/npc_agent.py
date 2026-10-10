#!/usr/bin/env python3
"""Greyspace NPC agent: simulated users browsing, resolving and mailing (app/greyspace/npc.py).

    python3 npc.py config.json

Each persona runs in its own thread: pick an action by the profile's weights, do it,
log one JSON line, sleep a random pause (divided by ``GS_NPC_SPEED``). Actions:

* ``browse``  GET a page of a site by name (HTTPS through the Greyspace CA when the
              stack has one, else HTTP), then follow one same-site link.
* ``resolve`` resolve a site name through the system resolver (the Greyspace resolver).
* ``mail``    send a short message over SMTP to another persona at a webmail domain.

Standard library only; never raises out of a persona loop (a failed action is logged).
"""

from __future__ import annotations

import json
import os
import random
import re
import smtplib
import socket
import ssl
import sys
import threading
import time
import urllib.request
from email.message import EmailMessage

UA = "Mozilla/5.0 (X11; Linux x86_64) GreyspaceNPC/1 ({persona})"
LINK = re.compile(r'href="(/[^"#?]*)"')


def log(**fields) -> None:
    fields.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    print(json.dumps(fields, sort_keys=True), flush=True)


class Persona(threading.Thread):
    def __init__(self, name: str, cfg: dict, speed: float, rng: random.Random):
        super().__init__(name=name, daemon=True)
        self.persona = name
        self.cfg = cfg
        self.speed = max(speed, 0.01)
        self.rng = rng
        self.ctx = None
        if cfg.get("https") and cfg.get("ca_file") and os.path.exists(cfg["ca_file"]):
            self.ctx = ssl.create_default_context(cafile=cfg["ca_file"])

    def pick(self) -> str:
        weights = {k: v for k, v in self.cfg["weights"].items() if v > 0}
        return self.rng.choices(list(weights), weights=list(weights.values()))[0]

    def get(self, url: str) -> tuple[int, str]:
        req = urllib.request.Request(url, headers={"User-Agent": UA.format(persona=self.persona)})
        with urllib.request.urlopen(req, timeout=10, context=self.ctx if url.startswith("https") else None) as r:
            return r.status, r.read(65536).decode("utf-8", "replace")

    def browse(self) -> dict:
        site = self.rng.choice(self.cfg["sites"])["fqdn"]
        scheme = "https" if self.ctx else "http"
        path = self.rng.choice(self.cfg["paths"])
        status, body = self.get(f"{scheme}://{site}{path}")
        links = sorted(set(LINK.findall(body)))
        followed = None
        if links:
            followed = self.rng.choice(links)
            self.get(f"{scheme}://{site}{followed}")
        return {"site": site, "path": path, "status": status, "followed": followed, "scheme": scheme}

    def resolve(self) -> dict:
        site = self.rng.choice(self.cfg["sites"])["fqdn"]
        addrs = sorted({a[4][0] for a in socket.getaddrinfo(site, 80, socket.AF_INET, socket.SOCK_STREAM)})
        return {"name": site, "addresses": addrs}

    def mail(self) -> dict:
        others = [p for p in self.cfg["personas"] if p != self.persona] or [self.persona]
        domain = self.rng.choice(self.cfg["mail_domains"])
        msg = EmailMessage()
        msg["From"] = f"{self.persona}@{domain}"
        msg["To"] = f"{self.rng.choice(others)}@{domain}"
        msg["Subject"] = self.rng.choice(["Lunch?", "Re: weekly report", "Meeting moved", "FYI", "Quick question"])
        msg["X-Greyspace-NPC"] = self.persona
        msg.set_content("Sent by a Greyspace simulated user.\n")
        with smtplib.SMTP(self.cfg["mail_host"], 25, timeout=10) as smtp:
            smtp.send_message(msg)
        return {"from": msg["From"], "to": msg["To"]}

    def run(self) -> None:
        lo, hi = self.cfg["pause"]
        time.sleep(self.rng.uniform(0, lo) / self.speed)
        while True:
            action = self.pick()
            try:
                detail = getattr(self, action)()
                log(persona=self.persona, action=action, ok=True, **detail)
            except Exception as exc:  # noqa: BLE001 — a failed action is traffic too; keep going
                log(persona=self.persona, action=action, ok=False, error=f"{type(exc).__name__}: {exc}"[:200])
            time.sleep(self.rng.uniform(lo, hi) / self.speed)


def main(argv: list[str]) -> int:
    cfg = json.load(open(argv[1], encoding="utf-8"))  # noqa: SIM115 — read once at start
    speed = float(os.environ.get("GS_NPC_SPEED", "1") or 1)
    seed = os.environ.get("GS_NPC_SEED")
    log(event="start", profile=cfg["profile"], personas=len(cfg["personas"]), sites=len(cfg["sites"]), speed=speed)
    for n, name in enumerate(cfg["personas"]):
        Persona(name, cfg, speed, random.Random(f"{seed}:{n}" if seed else None)).start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
