#!/bin/sh
set -eu
# Moodle compares PHP's SERVER_PORT with SITE_URL. The test harness publishes
# 8083:8080, so use the public port for direct requests without proxy headers.
# Run after the upstream image finishes rewriting its nginx configuration.
sed -i 's/default \$server_port;/default 8083;/' /etc/nginx/nginx.conf
grep -q 'default 8083;' /etc/nginx/nginx.conf
nginx -t
