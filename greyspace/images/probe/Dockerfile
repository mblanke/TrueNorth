# Greyspace probe: stands in for a range's edge in the T0 check (dig, curl, chronyd -Q through the stack).
FROM alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc
# APK_MIRROR: an http Alpine mirror (greyspace-host builds through the depot's
# apt-cacher-ng, which has no CONNECT); empty keeps the image's own repositories.
ARG APK_MIRROR=
RUN if [ -n "$APK_MIRROR" ]; then sed -i "s#^https://dl-cdn.alpinelinux.org/alpine#$APK_MIRROR#" /etc/apk/repositories; fi \
 && apk add --no-cache bind-tools curl iproute2 chrony \
 && if [ -n "$APK_MIRROR" ]; then sed -i "s#^$APK_MIRROR#https://dl-cdn.alpinelinux.org/alpine#" /etc/apk/repositories; fi
