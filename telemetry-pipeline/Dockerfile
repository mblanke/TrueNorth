FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
# setuptools and wheel are build tooling the base image ships; nothing imports them at
# runtime (the OTel 0.65b0 instrumentation no longer needs pkg_resources), and they
# carry CVEs (Trivy).
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y setuptools wheel
COPY . .
# Run as an unprivileged user (Trivy DS-0002). Dependencies and code stay root-owned,
# so a compromised process cannot rewrite the app it is running.
RUN useradd --system --uid 10001 --create-home --home-dir /home/app app
USER app
CMD ["python", "-m", "pipeline.ingest"]