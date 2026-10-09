FROM python:3.11-slim

WORKDIR /app

# Distro security updates at build time: the base tag lags them, and release.yml blocks
# on any fixable HIGH/CRITICAL (docs/release.md).
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# setuptools and wheel are build tooling the base image ships; nothing imports them at
# runtime, and they carry CVEs (Trivy).
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y setuptools wheel

# Allowlist, not `COPY . .`: only what the API runs and migrates with goes in the image,
# so a stray script or log in this directory cannot ship (2026-10 audit P0-7).
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
# Data the API reads at run time, from the repository's content/ (outside this build
# context, so it arrives as a named context):
#   docker build --build-context content=content control-plane/api
# GET /software-catalogue (app/routers/software_catalogue.py) and ATT&CK id checks
# (app/attack_catalogue.py) look in /app/content/... first. Shipped in the image so that
# Helm/k8s, which mounts no checkout, has them; compose no longer mounts them either.
COPY --from=content catalogue /app/content/catalogue
COPY --from=content mitre /app/content/mitre
# Release version (release.yml passes the git tag). The API's APP_VERSION constant
# does not read it yet; TN_VERSION is the interface for that.
ARG TN_VERSION=dev
ENV TN_VERSION=${TN_VERSION}
LABEL org.opencontainers.image.title="truenorth-api" \
      org.opencontainers.image.version="${TN_VERSION}" \
      org.opencontainers.image.source="https://github.com/mblanke/TrueNorth"
# Run as an unprivileged user (Trivy DS-0002). Dependencies and code stay root-owned,
# so a compromised process cannot rewrite the app it is running.
RUN useradd --system --uid 10001 --create-home --home-dir /home/app app
USER app

EXPOSE 8080

# Multi-worker uvicorn (4 workers for 1200-user scale)
# Workers = 2 * CPU_CORES + 1, tunable via UVICORN_WORKERS env
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers ${UVICORN_WORKERS:-4} --limit-concurrency 500 --timeout-keep-alive 30 --access-log"]