# TrueNorth AD test DC: a Samba 4 Active Directory domain controller for the CI `ad`
# lane and local identity work. NEVER joined to, or reachable from, a production
# domain; it provisions its own throwaway realm (default TNLAB.TEST) on every new
# volume. See tools/ad-test-dc/README.md.
#
# The base is pinned by digest (multi-arch index: amd64 for CI, arm64 for a Mac).
# Debian's samba packages come from that release's archive at build time.
FROM debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587

ENV DEBIAN_FRONTEND=noninteractive

# samba + samba-ad-provision: the AD DC (samba-tool domain provision). ldb-tools and
# ldap-utils: provisioning and the health check. socat + openssl: the "rogue CA" TLS
# front end the untrusted-CA test talks to (same image, different command).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      samba samba-ad-dc samba-ad-provision samba-dsdb-modules samba-vfs-modules \
      winbind ldb-tools ldap-utils socat openssl ca-certificates procps \
 && rm -rf /var/lib/apt/lists/* \
 && rm -f /etc/samba/smb.conf

COPY entrypoint.sh provision.sh /usr/local/sbin/
COPY accounts.tsv /usr/local/share/tn-ad-test-dc/accounts.tsv
RUN chmod 0755 /usr/local/sbin/entrypoint.sh /usr/local/sbin/provision.sh

# 636 only is used (LDAPS); nothing is published by compose.yml.
EXPOSE 636
VOLUME ["/var/lib/samba"]

HEALTHCHECK --interval=5s --timeout=5s --start-period=60s --retries=30 \
  CMD test -f /var/lib/samba/.tn-provisioned && LDAPTLS_REQCERT=never ldapsearch -x -H ldaps://localhost:636 -s base -b "" -LLL dn >/dev/null 2>&1 \
      || exit 1

ENTRYPOINT ["/usr/local/sbin/entrypoint.sh"]
