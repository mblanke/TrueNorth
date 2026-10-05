#!/bin/sh
set -eu
# Moodle compares PHP's SERVER_PORT with SITE_URL. A farm node publishes
# <public port>:8080, so report the public port for direct requests that carry
# no proxy headers. Run after the upstream image finishes rewriting its nginx
# configuration. MOODLE_PUBLIC_PORT defaults to the dev harness's 8083.
port="${MOODLE_PUBLIC_PORT:-8083}"
sed -i "s/default \$server_port;/default ${port};/" /etc/nginx/nginx.conf
grep -q "default ${port};" /etc/nginx/nginx.conf
nginx -t
