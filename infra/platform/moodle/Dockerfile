# A TrueNorth Moodle farm node: upstream Moodle with local_truenorth and its
# bootstrap baked in, so a node needs no internet access to come up wired.
#
#   docker build -t truenorth/moodle:5.2.3-tn infra/platform/moodle
#
# Hooks (run by the upstream entrypoint, in name order):
#   016-truenorth-plugin.sh     install/refresh local_truenorth before install/upgrade
#   04-truenorth-bootstrap.sh   SSO/sync key, LTI tool, login redirect, branding
#   99-public-port.sh           report MOODLE_PUBLIC_PORT to PHP
#
# release.yml publishes it as ghcr.io/mblanke/truenorth-moodle, by digest, scanned and
# signed like the other images; the installer runs it when tn_moodle_enabled
# (compose.moodle-prod.yml). The base is a single maintainer's image, so it is pinned by
# digest (the multi-arch index of v5.2.3): moving it is a reviewed change, not a re-tag.
FROM ghcr.io/erseco/alpine-moodle:v5.2.3@sha256:75dc2b3934d51122313674fbacf99702f23ffdc5511fb5f3b87fd45129cf1d1e

# moosh (a Moodle admin CLI) comes with the base image, with its development dependencies
# (PHPUnit, PHP_CodeSniffer, old Symfony) and their fixable HIGH vulnerabilities, which
# release.yml's scan blocks on. Nothing here runs it: the hooks use Moodle's own CLI.
RUN rm -rf /opt/moosh /usr/local/bin/moosh

COPY --chown=nobody:nobody local_truenorth /opt/truenorth/local_truenorth
COPY --chown=nobody:nobody bootstrap /opt/truenorth/bootstrap
COPY --chown=nobody:nobody truenorth.scss /opt/truenorth/truenorth.scss
COPY --chown=nobody:nobody --chmod=755 hooks/ /docker-entrypoint-init.d/
