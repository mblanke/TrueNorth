"""``python -m tn_noise_agent --controller URL --token-file PATH``"""

from __future__ import annotations

import argparse
import logging
import os
import ssl
import urllib.parse
from pathlib import Path

from .activities import AgentConfig
from .client import ControllerClient
from .runner import Runner


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="tn-noise", description="TrueNorth background-noise agent (white cell)")
    ap.add_argument("--controller", required=True, help="API root reachable over the management NIC")
    ap.add_argument("--token-file", required=True, type=Path)
    ap.add_argument("--ca-file", type=Path, help="CA bundle for the controller's certificate")
    ap.add_argument("--domain", default="corp.local", help="mail domain personas send from")
    ap.add_argument("--mgmt-net", action="append", default=[], help="management CIDR the agent must never target")
    ap.add_argument("--allow-http", action="store_true", help="permit a plain-http controller (lab use only)")
    ap.add_argument("--dry-run", action="store_true", help="plan and report, but touch no network")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if urllib.parse.urlsplit(args.controller).scheme != "https" and not args.allow_http:
        ap.error("the controller must be https (the token would travel in cleartext); --allow-http to override")
    if os.name == "posix" and args.token_file.stat().st_mode & 0o077:
        logging.warning("%s is readable by group/others; it should be 0600", args.token_file)
    ctx = ssl.create_default_context(cafile=str(args.ca_file)) if args.ca_file else None
    client = ControllerClient(args.controller, args.token_file.read_text().strip(), context=ctx)
    cfg = AgentConfig(
        domain=args.domain,
        forbidden_hosts=frozenset({(urllib.parse.urlsplit(args.controller).hostname or "").lower()}),
        forbidden_nets=tuple(args.mgmt_net),
    )
    runner = Runner(client, cfg, dry=args.dry_run)
    if args.once:
        runner.cycle()
    else:
        runner.run_forever()


if __name__ == "__main__":
    main()
