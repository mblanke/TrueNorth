# Greyspace recursive resolver: Unbound iterating from the Greyspace root (resolver/unbound.conf,
# resolver/root.hints are generated and mounted). Light: alpine + unbound only.
FROM alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc
# APK_MIRROR: an http Alpine mirror (greyspace-host builds through the depot's
# apt-cacher-ng, which has no CONNECT); empty keeps the image's own repositories.
ARG APK_MIRROR=
RUN if [ -n "$APK_MIRROR" ]; then sed -i "s#^https://dl-cdn.alpinelinux.org/alpine#$APK_MIRROR#" /etc/apk/repositories; fi \
 && apk add --no-cache unbound \
 && if [ -n "$APK_MIRROR" ]; then sed -i "s#^$APK_MIRROR#https://dl-cdn.alpinelinux.org/alpine#" /etc/apk/repositories; fi
EXPOSE 53/udp 53/tcp
ENTRYPOINT ["unbound", "-d", "-c", "/etc/unbound/unbound.conf"]
