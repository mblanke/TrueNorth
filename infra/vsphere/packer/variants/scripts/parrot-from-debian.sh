#!/usr/bin/env bash
# Author: Marc-Andre Blanke
# Created: 2026-11-23T20:47:00Z
# Variant script (parrot): convert a debian13 clone to Parrot Security 7 (echo).
# Runs as root after files/linux/apt-proxy.sh, so apt goes through the build apt proxy
# (TN-DEPOT01; its Remap-tnparrot serves deb.parrot.sh from an HTTPS mirror). Steps:
#   1. write Parrot's archive signing key, committed below, to its own keyring file and
#      check its fingerprint; every Parrot source is "signed-by" that file from the first
#      apt-get update, so nothing from Parrot is ever installed unauthenticated, and the
#      key is trusted for Parrot's sources only (an apt hook keeps it out of trusted.gpg.d);
#   2. install parrot-core (Parrot's apt pinning and base), then full-upgrade to Parrot;
#   3. install the MATE desktop and the full toolset, boot to the LightDM login.
# Fails the build on any error, if a Parrot key lands in /etc/apt/trusted.gpg.d, if a
# Parrot source is not signed-by the pinned key, or if the result is not Parrot or has no
# display manager.
#
# The key: "Parrot Project (2024-2026) <team@parrotsec.org>", fingerprint
# B711822346552E4D92DA02DF7A8286AF0E81EE4A (expires 2028-11-03). Checked on 2026-11-23
# against two official channels over HTTPS: the fingerprint published on
# https://parrotsec.org/docs/configuration/hash-and-key-verification/ and the key file
# https://deb.parrot.sh/parrot/misc/parrotsec.gpg (keyserver.ubuntu.com has the same key).
# When Parrot rotates its key, replace the block and PARROT_FPR together, from those sources.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
# Retries and no pipelining: the full toolset is ~2,800 packages through the depot, and under
# that load some fetches end "503 Connection closed" (site, 2026-11-24: 460 of them).
APT_OPTS=(-y -o Acquire::Retries=8 -o Acquire::http::Pipeline-Depth=0 -o Acquire::Queue-Mode=access
          -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)
LIST=/etc/apt/sources.list.d/parrot.list
KEY=/usr/share/keyrings/tn-parrot-archive.asc
PARROT_FPR="B711822346552E4D92DA02DF7A8286AF0E81EE4A"
COMPONENTS="main contrib non-free non-free-firmware"

# parrot-core and its upgrades ship their own https sources (https://deb.parrot.sh,
# https://mirror.parrot.sh/direct). The build proxy cannot tunnel HTTPS, so every Parrot
# source goes back to http://deb.parrot.sh, which the depot remaps to an HTTPS mirror.
# Packages stay verified by the pinned key (signed-by), not by the transport. Site fix
# 2026-11-24.
parrot_http() {
  local f
  for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
    [ -f "$f" ] || continue
    sed -i -e 's#https://mirror\.parrot\.sh/direct/parrot#http://deb.parrot.sh/parrot#g' \
           -e 's#https://deb\.parrot\.sh/parrot#http://deb.parrot.sh/parrot#g' "$f"
  done
  if grep -rqsE '^[^#]*https://[a-z.]*parrot' /etc/apt/sources.list /etc/apt/sources.list.d/; then
    echo "parrot: an https Parrot source is left; the build proxy cannot reach it:" >&2
    grep -rsE '^[^#]*https://[a-z.]*parrot' /etc/apt/sources.list /etc/apt/sources.list.d/ >&2
    exit 1
  fi
}

# A debian13 template built before cleanup.sh dropped the DVD source still lists it, and
# apt-get update fails on it.
sed -i "/^deb cdrom:/d" /etc/apt/sources.list 2>/dev/null || true
rm -f /etc/apt/sources.list.d/*cdrom* 2>/dev/null || true

# 1. The pinned key, checked before anything trusts it. gpg comes from Debian's own archive.
command -v gpg >/dev/null 2>&1 || { apt-get update -q && apt-get install "${APT_OPTS[@]}" gpg; }
cat > "$KEY" <<'PARROT_KEY'
-----BEGIN PGP PUBLIC KEY BLOCK-----

mQINBGco52cBEACWy0u0muKLZPH9OygsbRasVbQTNtUDLqdjd7MO8BGQfctdaQ81
msJYuBFcL8vyc5AbcpRyeBaZS2UlcWRsB4qu8xLgpvaaw5W/dE9mGQGBJV7Auyso
XOI83RnHup0+6T2h0DP7l3/ZQZjVt+Py0wIji9RiAAyYiW9rSFbedBsIWlD/h6xn
mXbMfVfD3uPiECpjERWQlo18T/gCNFe/p69oLCzIpLGtUCq+AIsgo9LL0vNV4zAt
+YCLlAlvtvNQo8DdAGOp11tIIDt7KFxXv4qfBXSi8WyZCyYD1vdtux7UKnx81NKC
7Fp441NL4dq3d5f1UQlceYWGj4F0yK6VCcE1jl1LekSp+j8I8rQgDP4CGmILCAAM
iYIsYfsF/WxSLKD4i7l3bYUrY8pzh8H/tDhW9/6pECTL01sD63CsccCllf4e7t15
mKj5zyTReAC5SqGJY7eCSSncMvclOtao2OrSpQNf1AD8x2i0TS2mtn6KrqsKrLv/
DBqfa9Lh6TxBe2csEkbSsupn6ECuL0UpUmD9oSELpqmhzL9i2oUNPIfUtEatCt5L
TGlWonG+xZu5CQGN1z3YkBAb6ZHxpTm0qks113ahzVlhfdbZvqeOO2csrQ7W6Vjb
FvxVSqEHU4QEd2O+oXsREvHxajDzm8WKfK+t3JIHkIeGWiExpzfhhcSL8wARAQAB
tC9QYXJyb3QgUHJvamVjdCAoMjAyNC0yMDI2KSA8dGVhbUBwYXJyb3RzZWMub3Jn
PokCVAQTAQoAPhYhBLcRgiNGVS5NktoC33qChq8Oge5KBQJnKOdnAhsDBQkHhM4A
BQsJCAcCBhUKCQgLAgQWAgMBAh4BAheAAAoJEHqChq8Oge5KCzIP/3UakIwwcdDA
3nsGuZgh5sVhEwRYyvKuN8r+YNYV8c08CMGt1V+eikJ96ZeT0IHvbgXAiwyukmTz
xYUO75Bbq+g0FE7ZLqrD62j2t+BzJNX4HBb9TTu5U5PXyXN0lpv5GHxTCrh6OsNv
lfZ1zyFYg7CB/gXF/7L5hDCqCKKLgFM82OnqVLTrZ9IpgWzsfPiLmRR3bPLuTX9O
RTqp9eaNcNCWy+WUvRm4oaod0JS76Xq2NT+fqwcAF5x4ziXAuH2rrF3DXwHvbKhF
p0v6BIph1U7iKQmRWaBrGE8EWvaUMaXaBThdOe3u+gU5DBX6c+XP5YY5ZlstNCUx
JN7m5BBDaQs8uQf8FveWAFh4PxyR7cCCaVrQArEV81UFAEyna86BVfzeSsafk5Ki
rGKeU4VRIGPn8XkMv2Nm4NDw8fCmy/6Lc3JryDsneNuPxTEMK9d0IlCQCrV5O0Kz
bMEB0QS2MFNA2KDPVO7IC/S/I9CA3Sr86CYZ7Ruv5b44IP8jfdfSmp+X37S9jOD1
rwlpmsV9isMA87lcSEdSEuOKXgaBdyuoeRREQATbPmQLyQMFUtqdIuO9cuvum5W7
56Ro4+GoCkXsYLVDFn/+9jodGE1ZOuEReF4tfCzedH1gQ9ex0FfdqwL6ZmfrOaLB
/stJyPk233PA9h03mutQza4yPT4GwAZ3
=jSUa
-----END PGP PUBLIC KEY BLOCK-----
PARROT_KEY
chmod 0644 "$KEY"
got="$(gpg --show-keys --with-colons "$KEY" 2>/dev/null | awk -F: '$1 == "fpr" {print $10}')"
[ "$got" = "$PARROT_FPR" ] || { echo "parrot: the committed key is not ${PARROT_FPR} (got: ${got:-none})" >&2; exit 1; }
cat > "$LIST" <<EOF
deb [signed-by=${KEY}] http://deb.parrot.sh/parrot echo ${COMPONENTS}
deb [signed-by=${KEY}] http://deb.parrot.sh/parrot echo-security ${COMPONENTS}
deb [signed-by=${KEY}] http://deb.parrot.sh/parrot echo-updates ${COMPONENTS}
EOF
# parrot-archive-keyring (pulled in by parrot-core) links its key into trusted.gpg.d from
# its postinst (ln -sf), which trusts Parrot's key for every source; a dpkg path-exclude
# cannot stop that. This hook removes it after every dpkg run - during this build and on
# every clone that later upgrades the package - so the key stays scoped to signed-by.
cat > /etc/apt/apt.conf.d/99-tn-parrot-key-scope <<'HOOK'
// TrueNorth: Parrot's archive key is trusted only through signed-by (sources.list.d/parrot.list).
DPkg::Post-Invoke { "rm -f /etc/apt/trusted.gpg.d/parrot* || true"; };
HOOK
echo "parrot: repository signed-by ${KEY} (${PARROT_FPR})"

# 2-3.
apt-get update -q
apt-get install "${APT_OPTS[@]}" parrot-core
parrot_http
apt-get update -q
apt-get full-upgrade "${APT_OPTS[@]}"
parrot_http
apt-get install "${APT_OPTS[@]}" parrot-interface parrot-desktop-mate parrot-tools-full open-vm-tools-desktop
parrot_http
apt-get autoremove -y
apt-get clean

# A maintainer script may still have linked a Parrot key into trusted.gpg.d: remove it, then
# refuse the template if one is left or a Parrot source is not pinned to our key.
rm -f /etc/apt/trusted.gpg.d/parrot* 2>/dev/null || true
if ls /etc/apt/trusted.gpg.d/parrot* >/dev/null 2>&1; then
  echo "parrot: a Parrot key is still in /etc/apt/trusted.gpg.d (trusted for every source)" >&2; exit 1
fi
unpinned="$(grep -rhs 'parrot' /etc/apt/sources.list /etc/apt/sources.list.d/*.list | grep -E '^[[:space:]]*deb' \
  | grep -v "signed-by=${KEY}" || true)"
[ -z "$unpinned" ] || { echo "parrot: Parrot sources not signed-by ${KEY}:" >&2; echo "$unpinned" >&2; exit 1; }
if grep -rlsi 'parrot' /etc/apt/sources.list.d/*.sources >/dev/null 2>&1; then
  echo "parrot: a deb822 Parrot source appeared; pin it to ${KEY} (Signed-By) before templating" >&2; exit 1
fi

grep -qi parrot /etc/os-release || { echo "parrot: /etc/os-release is not Parrot after the upgrade" >&2; cat /etc/os-release >&2; exit 1; }
systemctl list-unit-files lightdm.service --no-legend | grep -q lightdm || { echo "parrot: lightdm missing" >&2; exit 1; }
echo "/usr/sbin/lightdm" > /etc/X11/default-display-manager
systemctl enable lightdm.service
systemctl set-default graphical.target
echo "parrot: $(sed -n 's/^PRETTY_NAME=//p' /etc/os-release | tr -d '"'), default target $(systemctl get-default)"
