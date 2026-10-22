#!/bin/sh
# TrueNorth Range - VMware Tools (open-vm-tools) on the pfSense template and every range
# firewall cloned from it. Installed as /usr/local/etc/rc.d/tn-vmtools.sh by
# tn-pfsense-finish.sh. Started twice over, harmlessly: by tn-pfsense-config boot (the
# earlyshellcmd, so guestinfo is readable before the per-range config is applied) and by
# pfSense's package start-up, which runs every /usr/local/etc/rc.d/*.sh.
PIDFILE=/var/run/vmtoolsd.pid
VMTOOLSD=/usr/local/bin/vmtoolsd

running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

case "${1:-start}" in
  start)
    running && exit 0
    [ -x "$VMTOOLSD" ] || { echo "tn-vmtools: $VMTOOLSD missing" >&2; exit 1; }
    "$VMTOOLSD" -b "$PIDFILE"
    ;;
  stop)
    running && kill "$(cat "$PIDFILE")"
    rm -f "$PIDFILE"
    ;;
  restart)
    "$0" stop
    sleep 1
    "$0" start
    ;;
  status)
    if running; then echo "vmtoolsd running (pid $(cat "$PIDFILE"))"; else echo "vmtoolsd not running"; exit 1; fi
    ;;
  *)
    echo "usage: $0 start|stop|restart|status" >&2
    exit 2
    ;;
esac
