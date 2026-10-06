# A TrueNorth Moodle farm node: upstream Moodle with local_truenorth and its
# bootstrap baked in, so a node needs no internet access to come up wired.
#
#   docker build -t truenorth/moodle:5.2.3-tn infra/platform/moodle
#
# Hooks (run by the upstream entrypoint, in name order):
#   016-truenorth-plugin.sh     install/refresh local_truenorth before install/upgrade
#   04-truenorth-bootstrap.sh   SSO/sync key, LTI tool, login redirect, branding
#   99-public-port.sh           report MOODLE_PUBLIC_PORT to PHP
FROM ghcr.io/erseco/alpine-moodle:v5.2.3

COPY --chown=nobody:nobody local_truenorth /opt/truenorth/local_truenorth
COPY --chown=nobody:nobody bootstrap /opt/truenorth/bootstrap
COPY --chown=nobody:nobody truenorth.scss /opt/truenorth/truenorth.scss
COPY --chown=nobody:nobody --chmod=755 hooks/ /docker-entrypoint-init.d/
