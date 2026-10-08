FROM python:3.11-slim
WORKDIR /app
# Distro security updates at build time (release.yml blocks on fixable HIGH/CRITICAL).
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
# setuptools and wheel are build tooling the base image ships; nothing imports them at
# runtime, and they carry CVEs (Trivy).
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y setuptools wheel
COPY . .
ARG TN_VERSION=dev
ENV TN_VERSION=${TN_VERSION}
LABEL org.opencontainers.image.title="truenorth-scenario-engine" \
      org.opencontainers.image.version="${TN_VERSION}" \
      org.opencontainers.image.source="https://github.com/mblanke/TrueNorth"
# Run as an unprivileged user (Trivy DS-0002). Dependencies and code stay root-owned,
# so a compromised process cannot rewrite the app it is running.
RUN useradd --system --uid 10001 --create-home --home-dir /home/app app
USER app
EXPOSE 8082
CMD ["python", "-m", "runner.run", "--help"]