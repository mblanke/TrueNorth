#!/bin/bash
# TrueNorth AD test DC entrypoint.
#
#   entrypoint.sh            provision (first start of a new volume), then run samba
#   entrypoint.sh rogue      a TLS front end on :636 presenting a certificate from a CA
#                            Keycloak does NOT trust, forwarding to the DC's LDAPS. The
#                            untrusted-CA test points a federation test at it.
#
# Inputs (all read-only mounts, written on the host by tools/ad-test-dc/run.sh):
#   /secrets/<sAMAccountName>   one password per account, 0600 on the host; never printed
#   /certs/dc.crt dc.key ca.crt the DC's LDAPS certificate, its key, and the test CA's
#                               PUBLIC certificate. The CA's private key is not mounted.
#   /certs/rogue.crt rogue.key  (rogue mode only) the untrusted front end's pair
set -euo pipefail

REALM="${AD_REALM:-TNLAB.TEST}"
DOMAIN="${AD_DOMAIN:-TNLAB}"
HOST="${AD_DC_NETBIOS:-DC01}"
MARKER=/var/lib/samba/.tn-provisioned

log() { printf '[ad-test-dc] %s\n' "$*"; }

if [ "${1:-}" = "rogue" ]; then
  : "${AD_DC_ADDRESS:?AD_DC_ADDRESS (the DC name on the test network) is required}"
  install -d -m 0700 /run/rogue
  install -m 0600 /certs/rogue.key /run/rogue/rogue.key
  install -m 0644 /certs/rogue.crt /run/rogue/rogue.crt
  log "rogue TLS front end on :636 -> ${AD_DC_ADDRESS}:636"
  # verify=0: the front end does not authenticate its clients; it only presents the
  # untrusted certificate. Upstream is the DC's LDAPS (the DC's own cert is not checked:
  # nothing that matters ever completes through here).
  exec socat -d \
    "OPENSSL-LISTEN:636,reuseaddr,fork,cert=/run/rogue/rogue.crt,key=/run/rogue/rogue.key,verify=0" \
    "OPENSSL:${AD_DC_ADDRESS}:636,verify=0"
fi

for f in dc.crt dc.key ca.crt; do
  [ -s "/certs/$f" ] || { log "missing /certs/$f (run tools/ad-test-dc/run.sh certs)"; exit 2; }
done
[ -s /secrets/Administrator ] || { log "missing /secrets/Administrator (run tools/ad-test-dc/run.sh secrets)"; exit 2; }

# Samba refuses a TLS key that is not 0600 and owned by root; the mount is whatever the
# host made it, so it is copied in.
TLS=/var/lib/samba/private/tls-tn
install -d -m 0700 "$TLS"
install -m 0600 /certs/dc.key "$TLS/dc.key"
install -m 0644 /certs/dc.crt "$TLS/dc.crt"
install -m 0644 /certs/ca.crt "$TLS/ca.crt"

if [ ! -f "$MARKER" ]; then
  log "provisioning realm ${REALM} (NetBIOS ${DOMAIN}, DC ${HOST})"
  rm -f /etc/samba/smb.conf
  # xattr_tdb under acl_xattr: sysvol ACLs in a tdb, so no CAP_SYS_ADMIN for the
  # security.* xattrs a stock provision writes.
  # --adminpass on the command line is visible only inside this container's own process
  # table for the second the provision runs; the value is never echoed or logged.
  samba-tool domain provision \
    --use-rfc2307 \
    --realm="$REALM" \
    --domain="$DOMAIN" \
    --host-name="$HOST" \
    --server-role=dc \
    --dns-backend=SAMBA_INTERNAL \
    --option="vfs objects = dfs_samba4 acl_xattr xattr_tdb" \
    --adminpass="$(cat /secrets/Administrator)" \
    --option="tls enabled = yes" \
    --option="tls keyfile = $TLS/dc.key" \
    --option="tls certfile = $TLS/dc.crt" \
    --option="tls cafile = $TLS/ca.crt" \
    --option="dns forwarder = 127.0.0.11" \
    --option="dns update command = /bin/true" \
    >/var/log/tn-provision.log 2>&1 \
    || { log "provision failed:"; grep -v -i pass /var/log/tn-provision.log | tail -30; exit 1; }
  /usr/local/sbin/provision.sh
  # smb.conf lives in the container, the directory in the volume: keep the pair together
  # so a recreated container (new image, same volume) starts the same DC.
  cp /etc/samba/smb.conf /var/lib/samba/smb.conf.tn
  touch "$MARKER"
  log "provisioned"
else
  cp /var/lib/samba/smb.conf.tn /etc/samba/smb.conf
fi

log "starting samba (LDAPS on 636; nothing is published outside the test network)"
exec samba --foreground --no-process-group --debug-stdout --debuglevel="${AD_DEBUGLEVEL:-1}"
