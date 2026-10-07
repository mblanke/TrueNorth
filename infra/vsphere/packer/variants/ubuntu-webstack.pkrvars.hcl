# Variant: ubuntu-webstack — Ubuntu 24.04 LEMP web server.
# nginx + PHP-FPM + MariaDB, wired together so a range can drop a web app in at deploy.
# The application itself (and any scenario-specific content) is deploy-time, not baked.
#
# Metadata keys (variant_name .. variant_os_aliases) must each stay on ONE line:
# build.sh reads them to register the image.
variant_name        = "ubuntu-webstack"
variant_os_family   = "linux"
variant_base        = "ubuntu-lts"
variant_version     = "24.04"
variant_description = "Ubuntu 24.04 web server: nginx, PHP-FPM, MariaDB"
variant_os_aliases  = ["ubuntu-24.04-webstack"]

variant_apt_packages = [
  "nginx",
  "php-fpm",
  "php-mysql",
  "mariadb-server",
]

variant_scripts = ["webstack-enable.sh"]
