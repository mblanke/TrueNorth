#!/usr/bin/env python3
"""TrueNorth Range - VyOS template build, step 2 of 2 (live ISO, as root).

boot_command runs ``sudo python3 /run/tncd/tn-vyos-install.py`` from the build's second CD
(TNVYOS). It drives ``install image`` over a pseudo-terminal, answering each prompt by
what it says rather than by keystroke timing, then:

* gives the installed image a cloud-init datasource list that starts with VMware, so the
  per-range config the worker puts in guestinfo.userdata (worker/vyos_config.py) is read;
* powers the VM off. Packer (communicator "none") only waits for that, then templates it.

A prompt it has no answer for, an installer failure or a timeout: print the transcript and
do NOT power off. The build then fails at shutdown_timeout and no template is made.

The vyos user's password comes from tn-vyos.pw on the CD (the build's ssh_password; ADR
0012 decision 5's generic template login). Python 3 stdlib only; tested off-box with a fake
installer (TN_VYOS_INSTALLER, TN_VYOS_ROOT, TN_VYOS_NO_POWEROFF, TN_VYOS_STUCK_AFTER: test seams).
"""

from __future__ import annotations

import os
import pty
import re
import select
import shlex
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
INSTALLER = ["/usr/libexec/vyos/op_mode/image_installer.py", "--action", "install"]
TOTAL_TIMEOUT = 30 * 60      # the whole install
# A prompt nobody answers: give up after this long idle (TN_VYOS_STUCK_AFTER: tests).
STUCK_AFTER = float(os.environ.get("TN_VYOS_STUCK_AFTER", "120"))
SETTLE = 0.7                 # quiet time before a prompt is answered
MAX_SAME_ANSWER = 3          # the same prompt again and again (a rejected password): give up
CLOUD_CFG = "# TrueNorth: the per-range config arrives as VMware guestinfo (worker/vyos_config.py)\n" \
            "datasource_list: [ VMware, NoCloud, None ]\n"


def answer_for(pending: str, password: str) -> tuple[str, str] | None:
    """(rule, answer) for the prompt at the end of ``pending``, or None if there is none yet.

    A prompt is input() text the installer leaves without a newline; a finished line is
    output, never a prompt, whatever it says."""
    tail = pending.replace("\r", "")[-600:]
    if not tail.strip() or tail.endswith("\n"):
        return None
    last = tail.rstrip().splitlines()[-1] if tail.strip() else ""
    rules: list[tuple[str, str, str | None]] = [
        ("continue", r"Would you like to continue\?", "y"),
        ("image-name", r"name this image", ""),
        ("password-confirm", r"(confirm|retype|repeat)\b.*password", password),
        ("password", r"(enter|set)\b.*password", password),
        ("weak-password", r"(weak|short|strength).*\[y/n\]", "y"),
        ("console", r"What console should be used", "K"),
        ("disk", r"Which one should be used for installation", ""),
        ("raid", r"\bRAID\b", "n"),
        ("wipe", r"delete all data", "y"),
        ("all-space", r"use all the free space", "y"),
        ("ssh-keys", r"SSH host keys", "n"),     # clones must not share host keys
        ("copy-config", r"(active configuration|copy (it|the config))", "y"),
        ("boot-config", r"Which file would you like as boot config", None),
    ]
    for rule, pattern, answer in rules:
        if re.search(pattern, last, re.I):
            if answer is None:  # pick the saved live config (tn-vyos-prep.sh), not the factory one
                answer = ""
                for line in tail.splitlines():
                    m = re.match(r"\s*(\d+)[:.)]\s*(\S+)", line)
                    if m and m.group(2).endswith("/config/config.boot"):
                        answer = m.group(1)
                        break
            return rule, answer
    return None


def looks_like_prompt(pending: str) -> bool:
    text = pending.replace("\r", "")
    if not text.strip() or text.endswith("\n"):
        return False
    return bool(re.search(r"[?:\])]\s*$", text.splitlines()[-1]))


def drive(cmd: list[str], password: str, log=sys.stdout) -> str:
    """Run the installer under a pty, answering prompts. Returns the transcript."""
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(cmd[0], cmd)
    transcript, pending = "", ""
    last_output = start = time.monotonic()
    answered: dict[str, int] = {}
    status = None
    try:
        while True:
            now = time.monotonic()
            if now - start > TOTAL_TIMEOUT:
                raise RuntimeError("install image did not finish in time")
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(fd, 4096).decode("utf-8", errors="replace")
                except OSError:
                    chunk = ""
                if not chunk:
                    break
                transcript += chunk
                pending += chunk
                log.write(chunk)
                log.flush()
                last_output = time.monotonic()
                continue
            idle = now - last_output
            if pending and idle >= SETTLE:
                hit = answer_for(pending, password)
                if hit is not None:
                    rule, answer = hit
                    answered[rule] = answered.get(rule, 0) + 1
                    if answered[rule] > MAX_SAME_ANSWER:
                        raise RuntimeError(f"asked '{rule}' {answered[rule]} times; giving up")
                    shown = "<password>" if "password" in rule else repr(answer)
                    log.write(f"\n[tn-vyos-install] {rule}: {shown}\n")
                    os.write(fd, (answer + "\n").encode())
                    pending = ""
                    continue
            if pending and idle >= STUCK_AFTER and looks_like_prompt(pending):
                raise RuntimeError(f"no answer for prompt: {pending.strip().splitlines()[-1]!r}")
            done, st = os.waitpid(pid, os.WNOHANG)
            if done:
                status = st
                break
    except BaseException:
        _stop(pid)
        raise
    finally:
        os.close(fd)
    if status is None:
        _, status = os.waitpid(pid, 0)
    if os.waitstatus_to_exitcode(status) != 0:
        raise RuntimeError(f"install image exited {os.waitstatus_to_exitcode(status)}")
    if not re.search(r"installed successfully|installation (is )?complete", transcript, re.I):
        raise RuntimeError("install image ended without reporting success")
    return transcript


def _stop(pid: int) -> None:
    try:
        os.kill(pid, 15)
        os.waitpid(pid, 0)
    except (ProcessLookupError, ChildProcessError):
        pass


def finish_image(root: Path) -> Path:
    """Write the cloud-init datasource list into the installed image's persistent layer."""
    images = [p for p in (root / "boot").iterdir() if p.is_dir() and (p / "rw").is_dir()]
    if len(images) != 1:
        raise RuntimeError(f"expected one installed image under {root}/boot, found {[p.name for p in images]}")
    cfg = images[0] / "rw/etc/cloud/cloud.cfg.d/99-tn-vmware.cfg"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(CLOUD_CFG, encoding="utf-8")
    boot = images[0] / "rw/opt/vyatta/etc/config/config.boot"
    if boot.is_file() and "hw-id" in boot.read_text(encoding="utf-8", errors="replace"):
        print(f"[tn-vyos-install] warning: {boot} still pins hw-id (tn-vyos-prep.sh did not take)")
    (images[0] / "rw/etc/tn-template-finish.txt").write_text(
        f"finished={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\nimage={images[0].name}\n", encoding="utf-8")
    return cfg


def _sh(cmd: str) -> str:
    return subprocess.run(shlex.split(cmd), check=True, capture_output=True, text=True).stdout.strip()


def main() -> int:
    try:
        password = (HERE / "tn-vyos.pw").read_text(encoding="utf-8").strip()
        if not password:
            raise RuntimeError("tn-vyos.pw is empty")
        cmd = shlex.split(os.environ["TN_VYOS_INSTALLER"]) if os.environ.get("TN_VYOS_INSTALLER") else INSTALLER
        drive(cmd, password)
        test_root = os.environ.get("TN_VYOS_ROOT")
        if test_root:
            cfg = finish_image(Path(test_root))
        else:
            dev = _sh("blkid -L persistence")
            mnt = Path("/run/tnroot")
            mnt.mkdir(parents=True, exist_ok=True)
            subprocess.run(["mount", dev, str(mnt)], check=True)
            try:
                cfg = finish_image(mnt)
            finally:
                subprocess.run(["sync"], check=False)
                subprocess.run(["umount", str(mnt)], check=False)
        print(f"\n[tn-vyos-install] OK: installed; {cfg.name} written; powering off")
    except Exception as exc:  # noqa: BLE001 - any failure: say it, keep the VM up
        print(f"\n[tn-vyos-install] FAILED: {exc}")
        print("[tn-vyos-install] NOT powering off: the Packer build fails at shutdown_timeout (no template).")
        return 1
    if not os.environ.get("TN_VYOS_NO_POWEROFF"):
        subprocess.run(["systemctl", "poweroff"], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
