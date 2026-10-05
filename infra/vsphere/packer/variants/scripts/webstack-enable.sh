#!/usr/bin/env bash
# Variant script (ubuntu-webstack): wire nginx to PHP-FPM and enable the stack.
# Runs as root after the packages are installed. The web app and its database are
# deploy-time; this only leaves a working, empty LEMP server.
set -euo pipefail

# The php-fpm socket is versioned (php8.3-fpm.sock on 24.04); find whichever is installed.
fpm_service="$(systemctl list-unit-files 'php*-fpm.service' --no-legend | awk 'NR==1 {print $1}')"
if [ -z "$fpm_service" ]; then
  echo "webstack: no php-fpm service installed" >&2
  exit 1
fi
php_ver="${fpm_service#php}"
php_ver="${php_ver%-fpm.service}"
sock="/run/php/php${php_ver}-fpm.sock"

cat >/etc/nginx/sites-available/default <<EOF
# TrueNorth ubuntu-webstack default site: static files plus PHP via PHP-FPM.
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    root /var/www/html;
    index index.php index.html;
    server_name _;

    location / {
        try_files \$uri \$uri/ =404;
    }

    location ~ \.php\$ {
        include snippets/fastcgi-php.conf;
        fastcgi_pass unix:${sock};
    }

    location ~ /\.ht {
        deny all;
    }
}
EOF

cat >/var/www/html/index.php <<'EOF'
<?php
// TrueNorth ubuntu-webstack placeholder. Replaced by the range's application at deploy.
echo "TrueNorth web server ready\n";
EOF
rm -f /var/www/html/index.nginx-debian.html

nginx -t
systemctl enable nginx "$fpm_service" mariadb
echo "webstack: nginx -> ${sock}, services enabled"
