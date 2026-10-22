#!/usr/bin/env python3
"""TrueNorth Range - VyOS template build, step 2 of 2 (live ISO, as root).

boot_command runs ``sudo python3 /run/tncd/tn-vyos-install.py`` from the build's second CD
(TNVYOS). It drives ``install image`` over a pseudo-terminal, answering each prompt by
what it says rather than by keystroke timing, then:

* gives the installed image a cloud-init datasource list that starts with VMware, so the
  per-range config the worker puts in guestinfo.userdata (worker/vyos_config.py) is read;
* installs tn-admin-ack.service, which reports (guestinfo.tn.vyos.admin_ack) which password
  hash the vyos login has once that config is applied (issue #248);
* powers the VM off. Packer (communicator "none") only waits for that, then templates it.

A prompt it has no answer for, an installer failure or a timeout: print the transcript and
do NOT power off. The build then fails at shutdown_timeout and no template is made.

The vyos user's password comes from tn-vyos.pw on the CD (the build's ssh_password; ADR
0012 decision 5's generic template login). Python 3 stdlib only; tested off-box with a fake
installer (TN_VYOS_INSTALLER, TN_VYOS_ROOT, TN_VYOS_NO_POWEROFF, TN_VYOS_STUCK_AFTER: test seams).
"""

from __future__ import annotations

import hashlib
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
# Publishes the finish marker as guestinfo.tn.template.finish on every boot: the test
# clone's proof (scripts/lab/test-clone.sh --mode finish) that this finish ran.
MARKER_UNIT = """\
[Unit]
Description=TrueNorth: publish the template-finish marker to vSphere (guestinfo)
ConditionPathExists=/etc/tn-template-finish.txt
ConditionVirtualization=vmware
After=open-vm-tools.service

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'vmware-rpctool "info-set guestinfo.tn.template.finish $$(tr "\\n" " " < /etc/tn-template-finish.txt)"'
# ($$ is a literal $ for systemd.)

[Install]
WantedBy=multi-user.target
"""
# Issue #248: once cloud-init has applied the range's config (which gives the vyos login the
# range's own password as an encrypted-password hash, worker/vyos_config.py), report which
# hash the login has in force: guestinfo.tn.vyos.admin_ack = the first 16 hex digits of its
# sha256 (worker/appliance_admin.py fingerprint; never the hash), or "failed <why>".
# The template's own vyos hash has its fingerprint in /etc/tn-template-admin.fp (written by
# finish_image): while the login still has that one (cloud-init not done yet), it waits and
# looks again every TN_ACK_POLL seconds, at most TN_ACK_WAIT (10 min), then reports
# "failed the vyos password is still the template's". Without that file it reports at once.
# TN_RPCTOOL / TN_SHADOW / TN_TEMPLATE_FP / TN_ACK_*: test seams
# (tests/contracts/test_appliance_admin_hooks.py).
ACK_SCRIPT = """\
#!/bin/sh
RPC="${TN_RPCTOOL:-vmware-rpctool}"
SHADOW="${TN_SHADOW:-/etc/shadow}"
TEMPLATE_FP_FILE="${TN_TEMPLATE_FP:-/etc/tn-template-admin.fp}"
WAIT="${TN_ACK_WAIT:-600}"
POLL="${TN_ACK_POLL:-15}"
template_fp="$(cat "$TEMPLATE_FP_FILE" 2>/dev/null)"
waited=0
while :; do
  hash="$(awk -F: '$1 == "vyos" {print $2; exit}' "$SHADOW" 2>/dev/null)"
  case "$hash" in
    ''|'!'*|'*'*) fp="" ;;
    *) fp="$(printf '%s' "$hash" | sha256sum | cut -c1-16)" ;;
  esac
  if [ -n "$fp" ] && [ "$fp" != "$template_fp" ]; then
    ack="$fp"
    break
  fi
  if [ "$waited" -ge "$WAIT" ]; then
    if [ -z "$fp" ]; then
      ack="failed the vyos login has no password hash"
    else
      ack="failed the vyos password is still the template's"
    fi
    break
  fi
  sleep "$POLL"
  waited=$((waited + POLL))
done
exec "$RPC" "info-set guestinfo.tn.vyos.admin_ack $ack"
"""
# After cloud-final (itself after multi-user.target): so wanted by cloud-init.target, which
# avoids an ordering cycle with multi-user.target. Type=simple: its wait blocks no target.
ACK_UNIT = """\
[Unit]
Description=TrueNorth: report which password hash the vyos login has (guestinfo)
ConditionVirtualization=vmware
After=open-vm-tools.service cloud-final.service

[Service]
Type=simple
ExecStart=/bin/sh /etc/tn-admin-ack.sh

[Install]
WantedBy=cloud-init.target
"""


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


def admin_hash_of(config_boot: str) -> str | None:
    """The vyos login's encrypted-password in a config.boot (the template's own hash)."""
    m = re.search(r"\buser vyos \{.*?\bencrypted-password \"?([^\"\s]+)\"?", config_boot, re.S)
    return m.group(1) if m else None


def finish_image(root: Path) -> Path:
    """Write the cloud-init datasource list, the finish marker and the unit that publishes it
    into the installed image's persistent layer (its overlay's rw/ directory)."""
    images = [p for p in (root / "boot").iterdir() if p.is_dir() and (p / "rw").is_dir()]
    if len(images) != 1:
        raise RuntimeError(f"expected one installed image under {root}/boot, found {[p.name for p in images]}")
    cfg = images[0] / "rw/etc/cloud/cloud.cfg.d/99-tn-vmware.cfg"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(CLOUD_CFG, encoding="utf-8")
    boot = images[0] / "rw/opt/vyatta/etc/config/config.boot"
    if boot.is_file() and "hw-id" in boot.read_text(encoding="utf-8", errors="replace"):
        print(f"[tn-vyos-install] warning: {boot} still pins hw-id (tn-vyos-prep.sh did not take)")
    rw = images[0] / "rw"
    # admin_ack=1: this template acknowledges a range admin hash (issue #248); the worker
    # waits for that acknowledgement only from a template whose marker says so.
    (rw / "etc/tn-template-finish.txt").write_text(
        f"finished={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\nimage={images[0].name}\nadmin_ack=1\n",
        encoding="utf-8")
    template_hash = admin_hash_of(boot.read_text(encoding="utf-8", errors="replace")) if boot.is_file() else None
    if template_hash:
        fp = hashlib.sha256(template_hash.encode()).hexdigest()[:16]
        (rw / "etc/tn-template-admin.fp").write_text(fp + "\n", encoding="utf-8")
    else:
        print(f"[tn-vyos-install] warning: no vyos encrypted-password in {boot}; tn-admin-ack reports at once")
    unit = rw / "etc/systemd/system/tn-template-marker.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(MARKER_UNIT, encoding="utf-8")
    wants = rw / "etc/systemd/system/multi-user.target.wants"
    wants.mkdir(parents=True, exist_ok=True)
    link = wants / unit.name
    if not link.is_symlink():
        link.symlink_to("/etc/systemd/system/tn-template-marker.service")
    (rw / "etc/tn-admin-ack.sh").write_text(ACK_SCRIPT, encoding="utf-8")
    ack = rw / "etc/systemd/system/tn-admin-ack.service"
    ack.write_text(ACK_UNIT, encoding="utf-8")
    ack_wants = rw / "etc/systemd/system/cloud-init.target.wants"
    ack_wants.mkdir(parents=True, exist_ok=True)
    if not (ack_wants / ack.name).is_symlink():
        (ack_wants / ack.name).symlink_to("/etc/systemd/system/tn-admin-ack.service")
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
