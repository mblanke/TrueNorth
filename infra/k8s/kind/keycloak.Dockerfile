# The kind smoke's throwaway Keycloak (deps.yaml), pre-built so the pod never runs the
# Quarkus augmentation at start. `start-dev` (and plain `start`) re-augment on every
# container start because KC_HTTP_RELATIVE_PATH and KC_DB are build-time options; on a
# 2-vCPU CI runner shared with OpenSearch that took minutes and the container restarted
# mid-build (CrashLoopBackOff, PR #165 helm-kind). smoke.sh builds this on the runner and
# loads it into kind like the platform images; the pod runs `start --optimized`.
#
# The base is the pin compose.prod.yml uses (tests/contracts/test_helm_keycloak_mfa.py).
FROM quay.io/keycloak/keycloak:24.0@sha256:f8ade94c1d0ad2f2fa7734a455fee5392764f402c43ca35e9af6bf63a2541dc9

# dev-file: the embedded H2 database (throwaway cluster). Health on: the startup probe.
ENV KC_DB=dev-file \
    KC_HTTP_RELATIVE_PATH=/auth \
    KC_HEALTH_ENABLED=true
RUN /opt/keycloak/bin/kc.sh build
