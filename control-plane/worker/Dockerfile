FROM python:3.11-slim
WORKDIR /app
# Distro security updates at build time (release.yml blocks on fixable HIGH/CRITICAL).
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
# setuptools and wheel are build tooling the base image ships; nothing imports them at
# runtime, and they carry CVEs (Trivy).
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y setuptools wheel
# Inject dispatch (worker/inject_dispatch.py) runs scenario_engine's injectors; the worker
# no longer scores detections (ADR 0005). The engine lives outside this build context, so
# it arrives as a named context and only the package is copied:
#   docker build --build-context scenario_engine=scenario-engine control-plane/worker
# (compose: additional_contexts; CI: build-contexts). Its runtime needs (httpx, pyyaml)
# are already in requirements.txt.
COPY --from=scenario_engine scenario_engine /app/scenario_engine
# /app must be on sys.path for the whole life of the process, not just the cwd. `celery -A
# worker.celery_app` puts the cwd on sys.path only while it imports the app
# (celery.utils.imports.cwd_in_path) and removes it afterwards, so `worker` imported but
# every later lazy `import scenario_engine` (inject dispatch) failed with
# "No module named 'scenario_engine'" in the running worker.
ENV PYTHONPATH=/app
# Allowlist: the worker package only (2026-10 audit P0-7).
COPY worker ./worker
# Data the workers read at run time, from the repository's content/ (a named context too:
# --build-context content=content): the software catalogue vSphere installs resolve node
# services against (worker/software_catalogue.py) and the ATT&CK catalogue telemetry maps
# technique ids with (worker/attack_catalogue.py). Both look in /app/content/... first.
COPY --from=content catalogue /app/content/catalogue
COPY --from=content mitre /app/content/mitre
ARG TN_VERSION=dev
ENV TN_VERSION=${TN_VERSION}
LABEL org.opencontainers.image.title="truenorth-worker" \
      org.opencontainers.image.version="${TN_VERSION}" \
      org.opencontainers.image.source="https://github.com/mblanke/TrueNorth"
# Run as an unprivileged user (Trivy DS-0002). Dependencies and code stay root-owned,
# so a compromised process cannot rewrite the app it is running. Pre-created and owned
# by it: /data, where flower (same image, compose) keeps --db=/data/flower.db; a fresh
# named volume takes this directory's owner.
RUN useradd --system --uid 10001 --create-home --home-dir /home/app app \
    && mkdir -p /data \
    && chown app:app /data
USER app
CMD ["celery","-A","worker.celery_app","worker","--loglevel=INFO"]
