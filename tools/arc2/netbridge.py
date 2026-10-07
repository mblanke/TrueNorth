"""Inside a Linux job's private network namespace: the only routes out.

``bwrap --unshare-net`` gives a job a network namespace with nothing but its own
loopback, so it can reach no host, no localhost service and no internet. The runner
bind-mounts Unix sockets into the sandbox: one for the egress proxy (arc2/egress.py) and,
when configured, one per local port it may use (the model fallback). This bridge runs
first inside the sandbox, listens on 127.0.0.1:<port> for each mapping, forwards every
connection to its Unix socket, then runs the job's command and exits with its status.

Usage (built by confine.Bubblewrap.wrap, not by hand)::

    python -m arc2.netbridge --map 127.0.0.1:PORT=/path/to.sock [--map ...] -- cmd args...
"""

from __future__ import annotations

import argparse
import contextlib
import socket
import subprocess
import sys
import threading


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while data := src.recv(65536):
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            with contextlib.suppress(OSError):
                s.shutdown(socket.SHUT_RDWR)


def _serve(listener: socket.socket, unix_path: str) -> None:
    while True:
        try:
            client, _ = listener.accept()
        except OSError:
            return
        try:
            upstream = socket.socket(socket.AF_UNIX)
            upstream.connect(unix_path)
        except OSError:
            client.close()
            continue
        threading.Thread(target=_pipe, args=(client, upstream), daemon=True).start()
        threading.Thread(target=_pipe, args=(upstream, client), daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    split = argv.index("--") if "--" in argv else len(argv)
    ap = argparse.ArgumentParser(prog="arc2.netbridge")
    ap.add_argument("--map", action="append", default=[], help="127.0.0.1:PORT=/path/to.sock")
    args = ap.parse_args(argv[:split])
    cmd = argv[split + 1 :]
    if not cmd:
        ap.error("no command after --")
    for spec in args.map:
        addr, _, unix_path = spec.partition("=")
        host, _, port = addr.rpartition(":")
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host or "127.0.0.1", int(port)))
        listener.listen(64)
        threading.Thread(target=_serve, args=(listener, unix_path), daemon=True).start()
    return subprocess.run(cmd).returncode


if __name__ == "__main__":
    sys.exit(main())
