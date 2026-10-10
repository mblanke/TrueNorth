# Greyspace recursive resolver: Unbound iterating from the Greyspace root (resolver/unbound.conf,
# resolver/root.hints are generated and mounted). Light: alpine + unbound only.
FROM alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc
RUN apk add --no-cache unbound
EXPOSE 53/udp 53/tcp
ENTRYPOINT ["unbound", "-d", "-c", "/etc/unbound/unbound.conf"]
