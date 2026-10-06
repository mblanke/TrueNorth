#!/bin/sh
#
# Refresh local_truenorth from the image on every start.
#
# Runs after the upstream code sync (010) and before 02-configure-moodle.sh, which
# installs Moodle on a fresh volume or runs upgrade.php on an existing one; either
# way the plugin bundled in this image is what gets installed. The upstream
# PLUGINS hook downloads zips from URLs, which an air-gapped farm cannot do.
set -eu

src=/opt/truenorth/local_truenorth
if [ ! -d "$src" ]; then
    echo "[truenorth] no local_truenorth in this image; skipping"
    exit 0
fi
root=/var/www/html/public
[ -d "$root" ] || root=/var/www/html   # Moodle before 5.1 has no public/ web root
dest="$root/local/truenorth"

rm -rf "$dest"
mkdir -p "$(dirname "$dest")"
cp -R "$src" "$dest"
echo "[truenorth] installed local_truenorth $(sed -n "s/.*release *= *'\([^']*\)'.*/\1/p" "$src/version.php")"
