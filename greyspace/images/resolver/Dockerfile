# Greyspace recursive resolver: Unbound iterating from the Greyspace root (resolver/unbound.conf,
# resolver/root.hints are generated and mounted). Light: alpine + unbound only.
FROM alpine:3.20
RUN apk add --no-cache unbound
EXPOSE 53/udp 53/tcp
ENTRYPOINT ["unbound", "-d", "-c", "/etc/unbound/unbound.conf"]
