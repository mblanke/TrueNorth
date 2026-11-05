#!/usr/bin/env python3
"""Credential-free SSH transport for agent-exchange packages across the VPN."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

TARGET_RE = re.compile(r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9._:-]+$")
REMOTE_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class TransportError(Exception):
    pass


def _path(state_dir):
    return Path(state_dir) / "transport.local.json"


def _validate(config):
    target = str(config.get("ssh_target", ""))
    root = str(config.get("remote_root", ""))
    if not TARGET_RE.fullmatch(target) or target.startswith("-"):
        raise TransportError("ssh_target must be a plain [user@]host or IP")
    if not REMOTE_RE.fullmatch(root) or ".." in Path(root).parts:
        raise TransportError("remote_root must be a plain absolute POSIX path")
    timeout = int(config.get("connect_timeout_seconds", 10))
    if not 1 <= timeout <= 120:
        raise TransportError("connect_timeout_seconds must be between 1 and 120")
    return {"version": 1, "transport": "ssh", "ssh_target": target,
            "remote_root": root.rstrip("/"), "connect_timeout_seconds": timeout}


def _load(state_dir, explicit=None):
    path = Path(explicit).expanduser().resolve() if explicit else _path(state_dir)
    try:
        return _validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise TransportError(f"cannot read transport profile {path}: {exc}") from exc


def _ssh_base(config):
    return ["ssh", "-o", "BatchMode=yes", "-o",
            f"ConnectTimeout={config['connect_timeout_seconds']}", "--", config["ssh_target"]]


def _run(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TransportError(str(exc)) from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise TransportError(detail or f"command failed with exit {result.returncode}")
    return result


def cmd_init(args, state_dir):
    path = _path(state_dir)
    if path.exists() and not args.force:
        raise TransportError(f"profile already exists: {path} (use --force to replace)")
    config = _validate({"ssh_target": args.target, "remote_root": args.remote_root,
                        "connect_timeout_seconds": args.connect_timeout})
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    print(f"[exchange] local VPN transport profile -> {path}")


def cmd_probe(args, state_dir):
    config = _load(state_dir, args.config)
    root = config["remote_root"]
    _run(_ssh_base(config) + ["mkdir", "-p", f"{root}/inbox", f"{root}/outbox", f"{root}/processed"])
    _run(_ssh_base(config) + ["test", "-d", f"{root}/inbox"])
    print(json.dumps({"reachable": True, "target": config["ssh_target"],
                      "remote_root": root, "status": "ready"}))


def cmd_push(args, state_dir):
    config = _load(state_dir, args.config)
    source = Path(args.file).resolve()
    if not source.is_file():
        raise TransportError(f"file not found: {source}")
    if args.name and not NAME_RE.fullmatch(args.name):
        raise TransportError("remote name may contain only letters, digits, dot, underscore, and dash")
    name = args.name or source.name
    if not NAME_RE.fullmatch(name):
        raise TransportError(f"unsafe remote file name: {name}")
    root = config["remote_root"]
    _run(_ssh_base(config) + ["mkdir", "-p", f"{root}/inbox"])
    target = f"{config['ssh_target']}:{root}/inbox/{name}.partial"
    _run(["scp", "-q", "-o", "BatchMode=yes", "-o",
          f"ConnectTimeout={config['connect_timeout_seconds']}", "--", str(source), target])
    _run(_ssh_base(config) + ["mv", f"{root}/inbox/{name}.partial", f"{root}/inbox/{name}"])
    print(json.dumps({"pushed": str(source), "remote": f"inbox/{name}"}))


def cmd_pull(args, state_dir):
    config = _load(state_dir, args.config)
    if not NAME_RE.fullmatch(args.name):
        raise TransportError("remote name may contain only letters, digits, dot, underscore, and dash")
    destination = Path(args.destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    local = destination / args.name
    remote = f"{config['ssh_target']}:{config['remote_root']}/outbox/{args.name}"
    _run(["scp", "-q", "-o", "BatchMode=yes", "-o",
          f"ConnectTimeout={config['connect_timeout_seconds']}", "--", remote, str(local) + ".partial"])
    os.replace(str(local) + ".partial", local)
    print(json.dumps({"pulled": f"outbox/{args.name}", "local": str(local)}))


def execute(args, state_dir):
    if not shutil.which("ssh") or not shutil.which("scp"):
        raise SystemExit("[exchange] ssh and scp are required")
    try:
        return args.transport_func(args, Path(state_dir))
    except TransportError as exc:
        raise SystemExit(f"[exchange] {exc}") from exc
